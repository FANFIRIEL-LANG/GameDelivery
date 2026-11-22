from __future__ import annotations

import json
import logging
from typing import Any, Callable

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from core.delivery.playerok_eligibility import (
    PlayerokDeliveryBlocked,
    require_canonical_playerok_delivery_eligible,
)
from core.delivery.playerok_freshness import (
    PlayerokFreshnessBlocked,
    PlayerokFreshnessGate,
    PlayerokFreshnessProof,
    PlayerokOrderLease,
)
from core.delivery.send_policy import DeliverySendPolicy, ManualDispatchAuthorization
from games.asset_ids import asset_id_key
from integrations.asf.client import ASFClient, ASFError
from integrations.asf.trade_credentials import SteamTradeCredentialStore


logger = logging.getLogger(__name__)


class DeliveryDispatcher:
    """Idempotent, fail-closed dispatcher for already-reserved DeliveryLeg rows."""

    def __init__(
        self,
        database: Database,
        asf_client: ASFClient,
        credential_store: SteamTradeCredentialStore,
        *,
        restricted_buyer_message: str = (
            "Сейчас Steam не позволяет выполнить обмен: проверьте Steam Guard "
            "и торговые ограничения аккаунта. После устранения ограничения сообщите магазину."
        ),
        telegram_event: Callable[[str, dict[str, Any]], None] | None = None,
        send_policy: DeliverySendPolicy | None = None,
        playerok_freshness_gate: PlayerokFreshnessGate | None = None,
    ) -> None:
        self.database = database
        self.asf = asf_client
        self.credentials = credential_store
        self.restricted_buyer_message = restricted_buyer_message
        self.telegram_event = telegram_event
        self.send_policy = send_policy
        self.playerok_freshness_gate = playerok_freshness_gate
        self.ledger = DeliveryLedger(database)
        self._ensure_events()

    def auto_send_enabled(self) -> bool:
        return bool(self.send_policy and self.send_policy.auto_send_enabled())

    def _ensure_events(self) -> None:
        with self.database.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS gd_delivery_events (
                    event_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES gd_orders(order_id) ON DELETE RESTRICT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE(order_id, event_type)
                )
            """)

    @staticmethod
    def _event_id(order_id: str, event_type: str) -> str:
        import hashlib
        return "event:" + hashlib.sha256((order_id + ":" + event_type).encode()).hexdigest()[:24]

    def _event(self, order_id: str, event_type: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO gd_delivery_events(event_id,order_id,event_type,payload_json) "
                "VALUES(?,?,?,?)",
                (self._event_id(order_id, event_type), order_id, event_type, encoded),
            )
        if self.telegram_event and event_type == "telegram_notification":
            self.telegram_event(order_id, payload)

    @staticmethod
    def _classify_error(message: str) -> str:
        value = message.casefold()
        if any(term in value for term in (
            "private", "privacy", "inventory is private", "профиль закрыт",
            "инвентарь закрыт", "инвентарь получателя недоступен", "проверь приватность", "приватност", "приватность",
        )):
            return "PRIVACY_WAIT"
        if any(term in value for term in (
            "trade ban", "trade banned", "steam guard", "trade hold",
            "trade restricted", "not eligible to trade", "торговая блокировка",
            "steam guard", "торговые ограничения", "торговый бан", "удержание обмена", "ограничение/удержание обмена", "trade hold",
        )):
            return "TRADE_RESTRICTED"
        return ""

    def dispatch_leg(
        self,
        *,
        order_id: str,
        leg_id: str,
        stored_trade_url: str,
        retry_generation: int = 0,
        manual_authorization: ManualDispatchAuthorization | None = None,
        playerok_freshness_authorization: PlayerokFreshnessProof | None = None,
        playerok_order_lease: PlayerokOrderLease | None = None,
    ) -> dict[str, Any]:
        order, leg = order_id.strip(), leg_id.strip()
        if not order or not leg:
            raise ValueError("order_id и leg_id обязательны")
        if manual_authorization is None:
            if not self.auto_send_enabled():
                return {"dispatched": False, "status": "AUTO_SEND_DISABLED", "reason": "automatic dispatch is disabled"}
        elif not self.send_policy or not self.send_policy.validate_manual_authorization(
            manual_authorization, order, leg
        ):
            return {"dispatched": False, "status": "MANUAL_AUTHORIZATION_INVALID", "reason": "manual authorization is missing or expired"}

        recipient_from_url = self.credentials.recipient_steam_id64_from_url(stored_trade_url)
        trade_url = self.credentials.load_trade_url(order, stored_trade_url)
        authorization = {
            "mode": "manual" if manual_authorization is not None else "automatic",
        }
        if manual_authorization is not None:
            authorization["authorization_id"] = manual_authorization.authorization_id
        if isinstance(playerok_freshness_authorization, PlayerokFreshnessProof):
            authorization["playerok_status_checked_at"] = playerok_freshness_authorization.checked_at

        def claim_dispatch():
            with self.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    """
                    SELECT l.*, o.status AS order_status, o.recipient_steam_id64,
                           d.chat_id, ol.order_line_id, d.source_platform,
                           d.external_deal_id, d.playerok_account_id
                    FROM gd_delivery_legs l
                    JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
                    JOIN gd_orders o ON o.order_id=ol.order_id
                    JOIN gd_deals d ON d.deal_id=o.deal_id
                    WHERE l.leg_id=? AND o.order_id=?
                    """, (leg, order)
                ).fetchone()
                if row is None:
                    if self.playerok_freshness_gate is not None:
                        self.playerok_freshness_gate.revoke(playerok_freshness_authorization)
                    raise KeyError("DeliveryLeg не найден в указанном заказе")
                stored_recipient = row["recipient_steam_id64"]
                if stored_recipient is None or not str(stored_recipient).strip():
                    return {"_claimed": False, "response": {
                        "dispatched": False,
                        "status": "RECIPIENT_NOT_BOUND",
                        "reason": "Для заказа не закреплён Steam-получатель; требуется полный ASF preview.",
                    }}
                if str(stored_recipient) != str(recipient_from_url):
                    return {"_claimed": False, "response": {
                        "dispatched": False,
                        "status": "RECIPIENT_MISMATCH",
                        "reason": "Steam Trade URL указывает на другого получателя; отправка заблокирована.",
                    }}
                is_playerok = str(row["source_platform"] or "").strip().casefold() == "playerok"
                if (
                    is_playerok and self.playerok_freshness_gate is not None
                    and playerok_freshness_authorization is not None
                ):
                    try:
                        self.playerok_freshness_gate.consume_for_claim(
                            playerok_freshness_authorization,
                            account_user_id=str(row["playerok_account_id"] or ""),
                            deal_id=str(row["external_deal_id"] or ""),
                            order_id=order,
                            leg_id=leg,
                            lease=playerok_order_lease,
                            claim=lambda: True,
                        )
                    except PlayerokFreshnessBlocked as exc:
                        return {"_claimed": False, "response": {
                            "dispatched": False,
                            "status": "PLAYEROK_FRESHNESS_REQUIRED",
                            "reason": str(exc),
                        }}
                try:
                    require_canonical_playerok_delivery_eligible(db, order)
                except PlayerokDeliveryBlocked as exc:
                    if is_playerok and self.playerok_freshness_gate is not None:
                        self.playerok_freshness_gate.revoke(playerok_freshness_authorization)
                    return {"_claimed": False, "response": {
                        "dispatched": False,
                        "status": "PLAYEROK_NOT_ELIGIBLE",
                        "reason": str(exc),
                    }}
                if is_playerok and (
                    self.playerok_freshness_gate is None
                    or playerok_freshness_authorization is None
                ):
                    return {"_claimed": False, "response": {
                        "dispatched": False,
                        "status": "PLAYEROK_FRESHNESS_REQUIRED",
                        "reason": "Перед отправкой требуется свежая проверка Playerok через браузер.",
                    }}
                if row["order_status"] in {"UNKNOWN", "PRIVACY_WAIT", "TRADE_RESTRICTED", "DECLINED", "FAILED", "ACTION_REQUIRED", "DELIVERED"}:
                    return {"_claimed": False, "response": {"dispatched": False, "status": row["order_status"], "reason": "order is locked"}}
                if row["status"] in {"UNKNOWN", "SENT_TO_STEAM", "ACCEPTED", "DECLINED", "FAILED", "PRIVACY_WAIT", "TRADE_RESTRICTED"}:
                    return {"_claimed": False, "response": {"dispatched": False, "status": row["status"], "reason": "leg is already locked"}}
                if row["status"] not in {"reserved", "planned"}:
                    return {"_claimed": False, "response": {"dispatched": False, "status": row["status"], "reason": "leg is not dispatchable"}}
                if db.execute(
                    "SELECT 1 FROM gd_delivery_attempts "
                    "WHERE order_id=? AND status='dispatching' LIMIT 1",
                    (order,),
                ).fetchone():
                    return {"_claimed": False, "response": {"dispatched": False, "status": row["status"], "reason": "another leg of this order is dispatching"}}

                try:
                    asset_ids = json.loads(row["asset_ids_json"])
                except (TypeError, ValueError):
                    asset_ids = None
                if (
                    not isinstance(asset_ids, list) or not asset_ids
                    or any(not isinstance(value, str) or not value for value in asset_ids)
                    or len(set(asset_ids)) != len(asset_ids)
                ):
                    return {"_claimed": False, "response": {"dispatched": False, "status": row["status"], "reason": "invalid asset manifest"}}

                reservations = db.execute(
                    "SELECT asset_id FROM gd_resource_reservations "
                    "WHERE order_line_id=? AND bot_name=? COLLATE NOCASE "
                    "AND owner_type='order' AND app_id=? AND context_id=? AND state='active'",
                    (row["order_line_id"], row["bot_name"], int(row["app_id"]), str(row["context_id"])),
                ).fetchall()
                reserved_ids = [str(value["asset_id"]) for value in reservations]
                if sorted(reserved_ids) != sorted(asset_ids):
                    return {"_claimed": False, "response": {"dispatched": False, "status": row["status"], "reason": "active reservation mismatch"}}
                for asset_id in asset_ids:
                    active_count = db.execute(
                        "SELECT COUNT(*) FROM gd_resource_reservations "
                        "WHERE bot_name=? COLLATE NOCASE AND asset_id_key=? AND state IN ('active','legacy_conflict')",
                        (row["bot_name"], asset_id_key(asset_id)),
                    ).fetchone()[0]
                    if active_count != 1:
                        return {"_claimed": False, "response": {"dispatched": False, "status": row["status"], "reason": "asset has conflicting active reservations"}}

                attempt, created = self.ledger.create_delivery_attempt(
                    order_id=order, leg_id=leg, retry_generation=retry_generation,
                    status="prepared", result={"dispatch_authorization": authorization},
                    connection=db,
                )
                if not created:
                    try:
                        prior_attempt_result = json.loads(attempt.get("result_json") or "{}")
                    except (TypeError, ValueError):
                        prior_attempt_result = {}
                    approved_operator_retry = (
                        attempt.get("status") == "prepared"
                        and isinstance(prior_attempt_result, dict)
                        and bool(prior_attempt_result.get("operator_retry_action_id"))
                    )
                    if approved_operator_retry:
                        prior_attempt_result["dispatch_authorization"] = authorization
                        db.execute(
                            "UPDATE gd_delivery_attempts SET result_json=? WHERE attempt_id=?",
                            (json.dumps(prior_attempt_result, ensure_ascii=False, sort_keys=True),
                             attempt["attempt_id"]),
                        )
                    if not approved_operator_retry:
                        db.execute(
                            "UPDATE gd_delivery_legs SET status='UNKNOWN', "
                            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE leg_id=?",
                            (leg,),
                        )
                        db.execute(
                            "UPDATE gd_delivery_attempts SET status='unknown',result_json=? WHERE attempt_id=?",
                            (json.dumps({"reason": "idempotency key already exists; automatic retry prohibited"}), attempt["attempt_id"]),
                        )
                        db.execute(
                            "UPDATE gd_orders SET status='UNKNOWN',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                            (order,),
                        )
                        return {"_claimed": False, "response": {"dispatched": False, "status": "UNKNOWN", "reason": "idempotency key already exists"}}

                db.execute(
                    "UPDATE gd_delivery_attempts SET status='dispatching' WHERE attempt_id=?",
                    (attempt["attempt_id"],),
                )
                db.execute(
                    "UPDATE gd_delivery_legs SET status='DISPATCHING', "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE leg_id=?",
                    (leg,),
                )
                db.execute(
                    "UPDATE gd_orders SET status='PROCESSING', "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                    (order,),
                )
                return {"_claimed": True, "attempt": attempt, "row": dict(row), "asset_ids": asset_ids}

        if manual_authorization is None:
            authorized, claim = self.send_policy.run_auto_dispatch_claim(claim_dispatch) if self.send_policy else (False, None)
        else:
            authorized, claim = self.send_policy.run_manual_dispatch_claim(
                manual_authorization, order, leg, claim_dispatch
            )
        if not authorized:
            status = "AUTO_SEND_DISABLED" if manual_authorization is None else "MANUAL_AUTHORIZATION_INVALID"
            return {"dispatched": False, "status": status, "reason": "dispatch authorization failed"}
        if not claim["_claimed"]:
            return claim["response"]
        attempt, row, asset_ids = claim["attempt"], claim["row"], claim["asset_ids"]
        try:
            if manual_authorization is not None:
                logger.info(
                    "Manual delivery authorized; order_id=%s leg_id=%s authorization_id=%s",
                    order, leg, manual_authorization.authorization_id,
                )
            result = self.asf.send_trade_offer(
                order_id=order,
                bot_name=row["bot_name"],
                trade_url=trade_url,
                asset_ids=asset_ids,
                app_id=int(row["app_id"]),
                context_id=int(row["context_id"]),
            )
        except Exception as exc:
            # Once send_trade_offer has crossed its command boundary it returns resultUnknown.
            # A raised transport/timeout error here is conservatively treated as unknown too.
            self._set_state(order, leg, "UNKNOWN", "UNKNOWN", attempt["attempt_id"],
                            {"error_type": type(exc).__name__})
            self._event(order, "telegram_notification", {
                "reason": "UNKNOWN", "leg_id": leg, "error_type": type(exc).__name__,
            })
            return {"dispatched": False, "status": "UNKNOWN", "error_type": type(exc).__name__}

        if result.get("success") is True:
            offer_ids = result.get("offerIds")
            if not isinstance(offer_ids, list) or len(offer_ids) != 1:
                self._set_state(order, leg, "UNKNOWN", "UNKNOWN", attempt["attempt_id"],
                                {"reason": "ASF returned missing or ambiguous trade offer IDs", "result": result})
                return {"dispatched": False, "status": "UNKNOWN"}
            offer_id = str(offer_ids[0])
            self._set_state(order, leg, "SENT_TO_STEAM", "PROCESSING", attempt["attempt_id"],
                            {"trade_offer_id": offer_id, "result": result}, trade_offer_id=offer_id)
            return {"dispatched": True, "status": "SENT_TO_STEAM", "trade_offer_id": offer_id}

        message = str(result.get("error") or result.get("message") or "ASF не подтвердил отправку")
        classified = self._classify_error(message)
        if classified == "PRIVACY_WAIT":
            self._set_state(order, leg, "PRIVACY_WAIT", "PRIVACY_WAIT", attempt["attempt_id"],
                            {"error": message, "buyer_instruction": "!настроил приватность"})
            self._event(order, "buyer_privacy_instruction", {
                "chat_id": row["chat_id"],
                "message": "Откройте настройки приватности Steam и сделайте инвентарь доступным для просмотра. "
                           "После изменения напишите: !настроил приватность",
            })
            self._event(order, "telegram_notification", {
                "reason": "PRIVACY_WAIT", "leg_id": leg, "chat_id": row["chat_id"],
            })
            return {"dispatched": False, "status": "PRIVACY_WAIT"}
        if classified == "TRADE_RESTRICTED":
            self._set_state(order, leg, "TRADE_RESTRICTED", "TRADE_RESTRICTED", attempt["attempt_id"],
                            {"error": message, "buyer_message": self.restricted_buyer_message})
            self._event(order, "buyer_trade_restricted", {
                "chat_id": row["chat_id"], "message": self.restricted_buyer_message,
            })
            self._event(order, "telegram_notification", {
                "leg_id": leg, "reason": "TRADE_RESTRICTED", "error": message,
            })
            return {"dispatched": False, "status": "TRADE_RESTRICTED"}

        if result.get("resultUnknown") is True:
            self._set_state(order, leg, "UNKNOWN", "UNKNOWN", attempt["attempt_id"],
                            {"error_type": "ASFResultUnknown"})
            self._event(order, "telegram_notification", {
                "reason": "UNKNOWN", "leg_id": leg,
            })
            return {"dispatched": False, "status": "UNKNOWN"}

        self._set_state(order, leg, "FAILED", "ACTION_REQUIRED", attempt["attempt_id"], result)
        return {"dispatched": False, "status": "FAILED", "result": result}

    def _set_state(
        self, order_id: str, leg_id: str, leg_status: str, order_status: str,
        attempt_id: str, result: dict[str, Any], *, trade_offer_id: str | None = None,
    ) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT result_json FROM gd_delivery_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            try:
                prior_result = json.loads(previous["result_json"] or "{}") if previous else {}
            except (TypeError, ValueError):
                prior_result = {}
            merged_result = dict(result)
            if isinstance(prior_result, dict):
                for key in (
                    "dispatch_authorization", "operator_retry_action_id",
                    "operator_actor", "operator_reason", "recovery_evidence",
                ):
                    if key in prior_result:
                        merged_result.setdefault(key, prior_result[key])
            encoded = json.dumps(merged_result, ensure_ascii=False, sort_keys=True)
            db.execute(
                "UPDATE gd_delivery_legs SET status=?, trade_offer_id=COALESCE(?,trade_offer_id), "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE leg_id=?",
                (leg_status, trade_offer_id, leg_id),
            )
            db.execute(
                "UPDATE gd_delivery_attempts SET status=?, result_json=?, "
                "finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE attempt_id=?",
                (leg_status.casefold(), encoded, attempt_id),
            )
            effective_order_status = order_status
            if order_status == "ACTION_REQUIRED":
                in_flight = db.execute(
                    "SELECT 1 FROM gd_delivery_legs l "
                    "JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id "
                    "WHERE ol.order_id=? AND l.leg_id<>? "
                    "AND l.status IN ('UNKNOWN','SENT_TO_STEAM','DISPATCHING') LIMIT 1",
                    (order_id, leg_id),
                ).fetchone()
                if in_flight:
                    effective_order_status = "PROCESSING"
            db.execute(
                "UPDATE gd_orders SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE order_id=?",
                (effective_order_status, order_id),
            )
