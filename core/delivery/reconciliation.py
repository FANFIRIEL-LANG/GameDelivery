from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.database.db import Database
from integrations.asf.client import ASFClient


class DeliveryReconciliation:
    """Reconcile uncertain/sent legs against the ASF plugin and Steam offer state."""

    def __init__(
        self,
        database: Database,
        asf_client: ASFClient,
        *,
        sent_offers_path: str | Path | None = None,
    ) -> None:
        self.database = database
        self.asf = asf_client
        if sent_offers_path is None:
            local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
            sent_offers_path = Path(local) / "GameDelivery" / "ASFPlugin" / "sent_offers.json"
        self.sent_offers_path = Path(sent_offers_path)

    @staticmethod
    def recover_interrupted_dispatches(database: Database) -> int:
        """Run once during application startup, before any new dispatch begins."""
        with database.connect() as db:
            return db.execute(
                "UPDATE gd_delivery_legs SET status='UNKNOWN', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status='DISPATCHING'"
            ).rowcount

    def _sent_records(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.sent_offers_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _find_offer(self, order_id: str, bot_name: str, asset_ids: list[str]) -> str | None:
        matches: set[str] = set()
        for record in self._sent_records().values():
            if not isinstance(record, dict):
                continue
            if str(record.get("OrderId", record.get("orderId", ""))) != order_id:
                continue
            if str(record.get("BotName", record.get("botName", ""))).casefold() != bot_name.casefold():
                continue
            saved_assets = [str(value) for value in record.get("AssetIds", record.get("assetIds", []))]
            if saved_assets != asset_ids:
                continue
            ids = record.get("OfferIds", record.get("offerIds", []))
            if isinstance(ids, list):
                matches.update(str(value) for value in ids if str(value).isdigit() and int(value) > 0)
        return next(iter(matches)) if len(matches) == 1 else None

    def _status(self, bot_name: str, offer_id: str) -> dict[str, Any]:
        return self.asf.get_trade_offer_status(bot_name=bot_name, trade_offer_id=offer_id)

    def reconcile(self, *, order_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.connect() as db:
            sql = """
                SELECT l.*, ol.order_id
                FROM gd_delivery_legs l
                JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
                WHERE l.status IN ('UNKNOWN','SENT_TO_STEAM')
            """
            params: tuple[Any, ...] = ()
            if order_id:
                sql += " AND ol.order_id=?"
                params = (order_id,)
            rows = [dict(row) for row in db.execute(sql, params).fetchall()]

        results = []
        for leg in rows:
            order = leg["order_id"]
            offer_id = leg.get("trade_offer_id")
            if leg["status"] == "UNKNOWN":
                if not offer_id:
                    recover = getattr(self.asf, "get_order_trade_status", None)
                    if callable(recover):
                        try:
                            recovered = recover(bot_name=leg["bot_name"], order_id=order)
                            if recovered.get("success") is True and recovered.get("found") is True:
                                offer_id = str(recovered.get("tradeOfferId") or "")
                        except Exception:
                            # Fall back to the plugin's durable sent_offers.json manifest.
                            pass
                offer_id = offer_id or self._find_offer(
                    order, leg["bot_name"], json.loads(leg["asset_ids_json"])
                )
                if not offer_id:
                    results.append({"leg_id": leg["leg_id"], "status": "UNKNOWN", "recovered": False})
                    continue
                with self.database.connect() as db:
                    db.execute(
                        "UPDATE gd_delivery_legs SET trade_offer_id=?, status='SENT_TO_STEAM', "
                        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE leg_id=? AND status='UNKNOWN'",
                        (offer_id, leg["leg_id"]),
                    )
                leg["status"] = "SENT_TO_STEAM"
                leg["trade_offer_id"] = offer_id
            try:
                status = self._status(leg["bot_name"], str(offer_id))
            except Exception as exc:
                results.append({"leg_id": leg["leg_id"], "status": "UNKNOWN", "error": str(exc)})
                continue
            if status.get("success") is not True or status.get("found") is not True:
                results.append({"leg_id": leg["leg_id"], "status": "SENT_TO_STEAM", "steam_status": "unknown"})
                continue
            steam_state = str(status.get("status", "")).casefold()
            if steam_state == "accepted":
                self._update_leg(leg["leg_id"], "ACCEPTED")
                self._consume_reservations(leg["leg_id"])
                self._complete_order_if_all_accepted(order)
                results.append({"leg_id": leg["leg_id"], "status": "ACCEPTED", "trade_offer_id": offer_id})
            elif steam_state in {"declined", "canceled", "cancelled", "expired"}:
                self._update_leg(leg["leg_id"], "DECLINED")
                self._event(order, "telegram_notification", {
                    "leg_id": leg["leg_id"], "trade_offer_id": offer_id,
                    "steam_status": steam_state, "message": "Steam отклонил или завершил предложение обмена.",
                })
                try:
                    self.asf.get_bot_inventory(
                        leg["bot_name"], app_id=int(leg["app_id"]), context_id=int(leg["context_id"])
                    )
                    self._event(order, "inventory_refresh_requested", {
                        "bot_name": leg["bot_name"], "app_id": leg["app_id"],
                        "context_id": leg["context_id"],
                    })
                except Exception as exc:
                    self._event(order, "telegram_notification", {
                        "leg_id": leg["leg_id"], "inventory_refresh_error": str(exc)[:300],
                    })
                results.append({"leg_id": leg["leg_id"], "status": "DECLINED", "steam_status": steam_state})
            else:
                results.append({"leg_id": leg["leg_id"], "status": "SENT_TO_STEAM", "steam_status": status.get("status")})
        return results

    def _update_leg(self, leg_id: str, state: str) -> None:
        with self.database.connect() as db:
            db.execute(
                "UPDATE gd_delivery_legs SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE leg_id=?",
                (state, leg_id),
            )

    def _consume_reservations(self, leg_id: str) -> None:
        with self.database.connect() as db:
            db.execute("""
                UPDATE gd_resource_reservations SET state='consumed',
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE order_line_id=(
                    SELECT order_line_id FROM gd_delivery_legs WHERE leg_id=?
                ) AND bot_name=(
                    SELECT bot_name FROM gd_delivery_legs WHERE leg_id=?
                ) AND state='active' AND asset_id IN (
                    SELECT value FROM json_each(
                        (SELECT asset_ids_json FROM gd_delivery_legs WHERE leg_id=?)
                    )
                )
            """, (leg_id, leg_id, leg_id))

    def _complete_order_if_all_accepted(self, order_id: str) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            counts = db.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN l.status='ACCEPTED' THEN 1 ELSE 0 END) AS accepted
                FROM gd_delivery_legs l
                JOIN gd_order_lines ol ON ol.order_line_id=l.order_line_id
                WHERE ol.order_id=?
            """, (order_id,)).fetchone()
            if counts and counts["total"] > 0 and counts["total"] == counts["accepted"]:
                db.execute(
                    "UPDATE gd_orders SET status='DELIVERED', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE order_id=?", (order_id,)
                )

    @staticmethod
    def _event_id(order_id: str, event_type: str) -> str:
        import hashlib
        return "event:" + hashlib.sha256((order_id + ":" + event_type).encode()).hexdigest()[:24]

    def _event(self, order_id: str, event_type: str, payload: dict[str, Any]) -> None:
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
            db.execute(
                "INSERT OR IGNORE INTO gd_delivery_events(event_id,order_id,event_type,payload_json) VALUES(?,?,?,?)",
                (self._event_id(order_id, event_type), order_id, event_type,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
