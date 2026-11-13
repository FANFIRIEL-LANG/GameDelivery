from __future__ import annotations

import json
from typing import Any, Callable

from core.database.db import Database
from core.delivery.ledger import DeliveryLedger
from integrations.asf.client import ASFClient, ASFError
from integrations.asf.trade_credentials import SteamTradeCredentialStore


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
    ) -> None:
        self.database = database
        self.asf = asf_client
        self.credentials = credential_store
        self.restricted_buyer_message = restricted_buyer_message
        self.telegram_event = telegram_event
        self.ledger = DeliveryLedger(database)
        self._ensure_events()

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
    ) -> dict[str, Any]:
        order, leg = order_id.strip(), leg_id.strip()
        if not order or not leg:
            raise ValueError("order_id и leg_id обязательны")

        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """
                SELECT l.*, o.status AS order_status, d.chat_id
                FROM gd_delivery_legs l
                JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
                JOIN gd_orders o ON o.order_id=ol.order_id
                JOIN gd_deals d ON d.deal_id=o.deal_id
                WHERE l.leg_id=? AND o.order_id=?
                """, (leg, order)
            ).fetchone()
            if row is None:
                raise KeyError("DeliveryLeg не найден в указанном заказе")
            if row["order_status"] in {"UNKNOWN", "PRIVACY_WAIT", "TRADE_RESTRICTED", "DECLINED", "DELIVERED"}:
                return {"dispatched": False, "status": row["order_status"], "reason": "order is locked"}
            if row["status"] in {"UNKNOWN", "SENT_TO_STEAM", "ACCEPTED", "DECLINED", "PRIVACY_WAIT", "TRADE_RESTRICTED"}:
                return {"dispatched": False, "status": row["status"], "reason": "leg is already locked"}
            if row["status"] not in {"reserved", "planned"}:
                return {"dispatched": False, "status": row["status"], "reason": "leg is not dispatchable"}
            db.commit()

        attempt, created = self.ledger.create_delivery_attempt(
            order_id=order, leg_id=leg, retry_generation=retry_generation,
            status="prepared",
        )
        if not created:
            # A previous attempt may have reached Steam before the process stopped.
            self._set_state(order, leg, "UNKNOWN", "UNKNOWN", attempt["attempt_id"],
                            {"reason": "idempotency key already exists; automatic retry prohibited"})
            return {"dispatched": False, "status": "UNKNOWN", "reason": "idempotency key already exists"}

        try:
            trade_url = self.credentials.load_trade_url(order, stored_trade_url)
            with self.database.connect() as db:
                db.execute(
                    "UPDATE gd_delivery_attempts SET status='dispatching' WHERE attempt_id=?",
                    (attempt["attempt_id"],),
                )
                db.execute(
                    "UPDATE gd_delivery_legs SET status='DISPATCHING', "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE leg_id=?",
                    (leg,),
                )
            result = self.asf.send_trade_offer(
                order_id=order,
                bot_name=row["bot_name"],
                trade_url=trade_url,
                asset_ids=json.loads(row["asset_ids_json"]),
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

        self._set_state(order, leg, "FAILED", "FAILED", attempt["attempt_id"], result)
        return {"dispatched": False, "status": "FAILED", "result": result}

    def _set_state(
        self, order_id: str, leg_id: str, leg_status: str, order_status: str,
        attempt_id: str, result: dict[str, Any], *, trade_offer_id: str | None = None,
    ) -> None:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
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
            db.execute(
                "UPDATE gd_orders SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE order_id=?",
                (order_status, order_id),
            )
