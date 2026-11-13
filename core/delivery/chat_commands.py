from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from core.database.db import Database
from core.delivery.dispatcher import DeliveryDispatcher
from core.delivery.reconciliation import DeliveryReconciliation


@dataclass(frozen=True)
class ChatCommandResult:
    handled: bool
    command: str = ""
    status: str = ""
    message: str = ""


class BuyerChatCommandRegistry:
    """Extensible, order-scoped buyer commands with fail-closed privacy recovery."""

    BUILT_INS = {
        "!настроил приватность", "!статус", "!помощь",
        "!поддержка", "!неполучил", "!инвентарь",
    }

    DEFAULT_TEMPLATES = {
        "!помощь": "Команды: !статус, !поддержка, !неполучил, !инвентарь, !настроил приватность.",
        "!инвентарь": "Проверьте, что инвентарь Steam открыт для просмотра и предметы доступны для обмена.",
        "!поддержка": "Запрос передан оператору. Ожидайте ответа.",
        "!неполучил": "Сообщение о неполучении передано оператору.",
    }

    def __init__(
        self,
        database: Database,
        dispatcher: DeliveryDispatcher,
        reconciliation: DeliveryReconciliation,
        *,
        inventory_client: Any,
        custom_commands: dict[str, str] | None = None,
        status_renderer: Callable[[dict[str, Any]], str] | None = None,
        event_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.database = database
        self.dispatcher = dispatcher
        self.reconciliation = reconciliation
        self.asf = inventory_client
        self.templates = dict(self.DEFAULT_TEMPLATES)
        self.templates.update(self._normalize_custom(custom_commands or {}))
        self.status_renderer = status_renderer
        self.event_callback = event_callback
        self._ensure_events()

    @staticmethod
    def _normalize_custom(commands: dict[str, str]) -> dict[str, str]:
        normalized = {}
        for raw, template in commands.items():
            key = str(raw).strip().casefold()
            if not key.startswith("!") or len(key) > 80 or not isinstance(template, str):
                raise ValueError("Пользовательские команды должны иметь вид !команда и текстовый шаблон")
            if key in BuyerChatCommandRegistry.BUILT_INS:
                raise ValueError("Нельзя переопределять защищённые встроенные команды")
            normalized[key] = template[:2000]
        return normalized

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
                    UNIQUE(order_id,event_type)
                )
            """)

    def _event(self, order_id: str, kind: str, payload: dict[str, Any]) -> None:
        import hashlib
        event_id = "event:" + hashlib.sha256((order_id + ":" + kind).encode()).hexdigest()[:24]
        with self.database.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO gd_delivery_events(event_id,order_id,event_type,payload_json) VALUES(?,?,?,?)",
                (event_id, order_id, kind, json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
        if self.event_callback:
            self.event_callback(order_id, {"event_type": kind, **payload})

    @staticmethod
    def _payload(row: Any) -> dict[str, Any]:
        try:
            value = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _order_context(self, *, chat_id: str, deal_id: str) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute("""
                SELECT o.order_id,o.status AS order_status,o.total_quantity,
                       d.deal_id,d.chat_id,d.payload_json,d.source_platform,d.external_deal_id
                FROM gd_orders o JOIN gd_deals d ON d.deal_id=o.deal_id
                WHERE d.deal_id=? AND d.chat_id=?
            """, (deal_id, chat_id)).fetchone()
        return dict(row) if row else None

    def handle(
        self,
        *,
        text: str,
        chat_id: str,
        deal_id: str,
        buyer_identity: str | None = None,
        identity_verified: bool = False,
    ) -> ChatCommandResult:
        raw = str(text or "").strip()
        if not raw:
            return ChatCommandResult(False)
        lowered = raw.casefold()
        command = next(
            (candidate for candidate in sorted(
                self.BUILT_INS | set(self.templates), key=len, reverse=True
            ) if lowered == candidate or lowered.startswith(candidate + " ")),
            "",
        )
        if not command:
            return ChatCommandResult(False, lowered.split(maxsplit=1)[0])
        context = self._order_context(chat_id=str(chat_id), deal_id=str(deal_id))
        if context is None:
            return ChatCommandResult(False, command, "unmatched", "")
        order_id = context["order_id"]
        if command == "!настроил приватность":
            return self._privacy_ready(
                context, buyer_identity=buyer_identity, identity_verified=identity_verified
            )
        if command not in self.BUILT_INS and command not in self.templates:
            return ChatCommandResult(False, command)
        if command == "!статус":
            message = (
                self.status_renderer(context) if self.status_renderer
                else f"Статус заказа: {context['order_status']}."
            )
            return ChatCommandResult(True, command, context["order_status"], message)
        if command in {"!поддержка", "!неполучил"}:
            self._event(order_id, "operator_support_request", {
                "deal_id": deal_id, "chat_id": chat_id, "command": command,
                "message": raw[:1000],
            })
            return ChatCommandResult(True, command, context["order_status"], self.templates[command])
        message = self.templates.get(command, "")
        return ChatCommandResult(True, command, context["order_status"], message)

    def _privacy_ready(
        self, context: dict[str, Any], *, buyer_identity: str | None,
        identity_verified: bool,
    ) -> ChatCommandResult:
        order_id, deal_id = context["order_id"], context["deal_id"]
        payload = self._payload(context)
        expected_buyer = str(payload.get("buyer_steam_id64") or payload.get("buyer_id") or "")
        if not identity_verified or not buyer_identity or not expected_buyer or str(buyer_identity) != expected_buyer:
            return ChatCommandResult(True, "!настроил приватность", "ignored_identity", "")
        if context["order_status"] != "PRIVACY_WAIT":
            return ChatCommandResult(True, "!настроил приватность", "stale_ignored", "")

        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            legs = [dict(row) for row in db.execute("""
                SELECT l.* FROM gd_delivery_legs l
                JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
                WHERE ol.order_id=?
            """, (order_id,)).fetchall()]
            attempts = [dict(row) for row in db.execute(
                "SELECT * FROM gd_delivery_attempts WHERE order_id=?", (order_id,)
            ).fetchall()]
            if not legs or any(
                leg["trade_offer_id"] or leg["status"] in {"UNKNOWN", "SENT_TO_STEAM", "ACCEPTED", "DELIVERED"}
                for leg in legs
            ) or any(attempt["status"] in {"unknown", "sent_to_steam", "dispatching"} for attempt in attempts):
                db.commit()
                self.reconciliation.reconcile(order_id=order_id)
                return ChatCommandResult(True, "!настроил приватность", "reconciliation_required", "")
            privacy_legs = [leg for leg in legs if leg["status"] == "PRIVACY_WAIT"]
            if len(privacy_legs) != 1:
                db.commit()
                return ChatCommandResult(True, "!настроил приватность", "blocked_ambiguous", "")
            leg = privacy_legs[0]
            asset_ids = json.loads(leg["asset_ids_json"])
            try:
                inventory = self.asf.get_bot_inventory(
                    leg["bot_name"], app_id=int(leg["app_id"]), context_id=int(leg["context_id"])
                )
                if not isinstance(inventory, dict):
                    raise ValueError("ASF не подтвердил успешное получение свежего inventory")
                success_key = next((key for key in inventory if str(key).casefold() == "success"), None)
                if success_key is None or inventory[success_key] is not True:
                    raise ValueError("ASF не подтвердил успешное получение свежего inventory")
                result_key = next((key for key in inventory if str(key).casefold() == "result"), None)
                root = inventory[result_key] if result_key is not None else inventory
                bot = next((v for k, v in root.items() if str(k).casefold() == leg["bot_name"].casefold()), None) if isinstance(root, dict) else None
                assets_key = next((key for key in bot if str(key).casefold() == "assets"), None) if isinstance(bot, dict) else None
                assets = bot[assets_key] if assets_key is not None else None
                if not isinstance(assets, list):
                    raise ValueError("ASF не подтвердил полный свежий inventory")
                indexed = {}
                for asset in assets:
                    if not isinstance(asset, dict):
                        continue
                    id_key = next((key for key in asset if str(key).casefold() == "assetid"), None)
                    if id_key is not None:
                        indexed[str(asset[id_key])] = asset
                for asset_id in asset_ids:
                    item = indexed.get(str(asset_id))
                    tradable_key = next((key for key in item if str(key).casefold() == "tradable"), None) if isinstance(item, dict) else None
                    tradable = item[tradable_key] if tradable_key is not None else None
                    if tradable not in (True, 1, "1", "true", "True"):
                        raise ValueError("Зарезервированный asset отсутствует у бота или не tradable")
            except Exception as exc:
                db.commit()
                return ChatCommandResult(True, "!настроил приватность", "inventory_recheck_failed", str(exc)[:300])

            retry_generation = max(
                (int(attempt["retry_generation"]) for attempt in attempts if attempt["leg_id"] == leg["leg_id"]),
                default=-1,
            ) + 1
            db.execute(
                "UPDATE gd_delivery_legs SET status='reserved',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE leg_id=?",
                (leg["leg_id"],),
            )
            db.execute(
                "UPDATE gd_orders SET status='PROCESSING',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE order_id=?",
                (order_id,),
            )
            db.commit()

        safe_trade_url = str(payload.get("stored_trade_url") or payload.get("trade_url_safe") or "")
        result = self.dispatcher.dispatch_leg(
            order_id=order_id, leg_id=leg["leg_id"],
            stored_trade_url=safe_trade_url, retry_generation=retry_generation,
        )
        return ChatCommandResult(
            True, "!настроил приватность", str(result.get("status", "unknown")),
            "Повторная попытка обработки заказа запущена." if result.get("dispatched")
            else "Не удалось безопасно продолжить обработку; заказ сохранён для проверки.",
        )
