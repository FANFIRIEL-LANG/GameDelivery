from __future__ import annotations

import json
import os
import hashlib
from typing import Any, Callable


class TelegramCredentialStore:
    """Store bot token and proxy credentials only in the OS keyring."""

    SERVICE = "GameDelivery.Telegram"

    def __init__(self, backend: Any | None = None):
        if backend is None:
            try:
                import keyring
                backend = keyring.get_keyring()
            except Exception:
                raise OSError("Системное хранилище Telegram credentials недоступно") from None
        self.backend = backend

    def set(self, name: str, value: str) -> None:
        if not name or not value:
            raise ValueError("Credential name and value are required")
        try:
            self.backend.set_password(self.SERVICE, name, value)
            if self.backend.get_password(self.SERVICE, name) != value:
                raise RuntimeError("keyring verification failed")
        except Exception:
            raise OSError("Не удалось сохранить Telegram credentials в системном keyring") from None

    def get(self, name: str) -> str | None:
        try:
            return self.backend.get_password(self.SERVICE, name)
        except Exception:
            raise OSError("Не удалось прочитать Telegram credentials из системного keyring") from None

    def configure(self, *, bot_token: str, proxy_url: str | None = None,
                  proxy_username: str | None = None, proxy_password: str | None = None) -> None:
        self.set("bot_token", bot_token)
        if proxy_url:
            self.set("proxy_url", proxy_url)
        if proxy_username:
            self.set("proxy_username", proxy_username)
        if proxy_password:
            self.set("proxy_password", proxy_password)

    def proxy(self) -> dict[str, str] | None:
        url = self.get("proxy_url")
        if not url:
            return None
        return {
            "url": url,
            "username": self.get("proxy_username") or "",
            "password": self.get("proxy_password") or "",
        }


def telegram_api_sender(token: str, payload: dict[str, Any], proxy: dict[str, str] | None = None) -> dict[str, Any]:
    """Send one Telegram Bot API request; supports HTTP(S) and SOCKS5 proxies."""
    import requests
    from urllib.parse import quote, urlsplit, urlunsplit

    if not token or any(ord(ch) < 32 for ch in token):
        raise ValueError("Telegram bot token is invalid")
    data = dict(payload)
    if "chat_id" not in data:
        raise ValueError("Telegram payload must include chat_id")
    endpoint = "https://api.telegram.org/bot" + token + "/sendMessage"
    proxies = None
    if proxy and proxy.get("url"):
        raw = proxy["url"]
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
            raise ValueError("Telegram proxy must use HTTP(S) or SOCKS5")
        if proxy.get("username"):
            host = parsed.hostname
            if parsed.port:
                host += ":" + str(parsed.port)
            auth = quote(proxy["username"], safe="") + ":" + quote(proxy.get("password", ""), safe="")
            raw = urlunsplit((parsed.scheme, auth + "@" + host, parsed.path, parsed.query, ""))
        proxies = {"http": raw, "https": raw}
    try:
        response = requests.post(endpoint, json=data, proxies=proxies, timeout=(5, 20))
        response.raise_for_status()
        result = response.json()
    except Exception as exc:
        # Do not propagate exception strings: request URLs may contain the bot token.
        raise RuntimeError("Telegram API request failed: " + type(exc).__name__) from None
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram API did not confirm message delivery")
    return result


class TelegramBotOutbox:
    """Durable non-blocking Telegram notification queue with idempotent retries."""

    def __init__(
        self, database, credentials: TelegramCredentialStore,
        sender: Callable[[str, dict[str, Any], dict[str, str] | None], Any],
        *, event_filters: dict[str, bool] | None = None,
    ):
        self.database = database
        self.credentials = credentials
        self.sender = sender
        self.event_filters = dict(event_filters or {})
        self._ensure_schema()

    def _ensure_schema(self):
        with self.database.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_outbox (
                    outbox_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    order_id TEXT,
                    event_type TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error TEXT,
                    telegram_message_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS telegram_operator_messages (
                    telegram_message_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL REFERENCES gd_orders(order_id) ON DELETE RESTRICT,
                    deal_id TEXT NOT NULL REFERENCES gd_deals(deal_id) ON DELETE RESTRICT,
                    chat_id TEXT NOT NULL,
                    operator_chat_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(telegram_operator_messages)").fetchall()}
            if "operator_chat_id" not in columns:
                db.execute("ALTER TABLE telegram_operator_messages ADD COLUMN operator_chat_id TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS idx_telegram_outbox_pending ON telegram_outbox(status,next_attempt_at)")

    def enqueue(
        self, *, event_type: str, chat_id: str, payload: dict[str, Any],
        order_id: str | None = None, idempotency_key: str | None = None,
    ) -> str | None:
        if self.event_filters.get(event_type, True) is False:
            return None
        key = idempotency_key or hashlib.sha256(
            (str(order_id or "") + "|" + event_type + "|" + chat_id + "|" +
             json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode()
        ).hexdigest()
        outbox_id = "tg:" + hashlib.sha256(key.encode()).hexdigest()[:32]
        with self.database.connect() as db:
            db.execute("""
                INSERT OR IGNORE INTO telegram_outbox
                    (outbox_id,idempotency_key,order_id,event_type,chat_id,payload_json)
                VALUES (?,?,?,?,?,?)
            """, (outbox_id, key, order_id, event_type, str(chat_id),
                  json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        return outbox_id

    def deliver_once(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("limit должен быть от 1 до 100")
        with self.database.connect() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT * FROM telegram_outbox
                WHERE status IN ('pending','retry')
                  AND (next_attempt_at IS NULL OR next_attempt_at <= strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ORDER BY created_at LIMIT ?
            """, (limit,)).fetchall()]
        results = []
        token = self.credentials.get("bot_token")
        if not token:
            return [{"outbox_id": row["outbox_id"], "status": "pending", "error": "Telegram token is not configured"} for row in rows]
        proxy = self.credentials.proxy()
        for row in rows:
            try:
                response = self.sender(token, json.loads(row["payload_json"]), proxy)
                message_id = None
                if isinstance(response, dict):
                    result = response.get("result", response)
                    if isinstance(result, dict):
                        message_id = result.get("message_id")
                with self.database.connect() as db:
                    db.execute("""
                        UPDATE telegram_outbox SET status='sent',telegram_message_id=?,
                            attempts=attempts+1,last_error=NULL,
                            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE outbox_id=?
                    """, (str(message_id) if message_id is not None else None, row["outbox_id"]))
                if message_id is not None and row.get("order_id"):
                    try:
                        self.bind_operator_message(
                            telegram_message_id=str(message_id), order_id=str(row["order_id"]),
                            operator_chat_id=str(row["chat_id"]),
                        )
                    except Exception:
                        # Telegram delivery is already confirmed; do not resend a duplicate
                        # merely because local reply routing metadata could not be stored.
                        results.append({"outbox_id": row["outbox_id"], "status": "sent", "message_id": message_id,
                                        "reply_binding": "failed"})
                        continue
                results.append({"outbox_id": row["outbox_id"], "status": "sent", "message_id": message_id})
            except Exception as exc:
                # Telegram is an auxiliary channel: record failure and continue the delivery pipeline.
                with self.database.connect() as db:
                    db.execute("""
                        UPDATE telegram_outbox SET status='retry',attempts=attempts+1,
                            last_error=?,next_attempt_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','+60 seconds'),
                            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE outbox_id=?
                    """, (type(exc).__name__, row["outbox_id"]))
                results.append({"outbox_id": row["outbox_id"], "status": "retry"})
        return results

    def enqueue_pending_delivery_events(self, *, operator_chat_id: str, limit: int = 50) -> int:
        """Transfer pending delivery/operator events into Telegram's durable outbox."""
        if limit < 1 or limit > 500:
            raise ValueError("limit должен быть от 1 до 500")
        with self.database.connect() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT event_id,order_id,event_type,payload_json
                FROM gd_delivery_events
                WHERE status='pending'
                  AND event_type NOT LIKE 'buyer_%'
                ORDER BY created_at LIMIT ?
            """, (limit,)).fetchall()]
        queued = 0
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = {"details": "invalid event payload"}
            raw_category = str(
                payload.get("reason") or payload.get("status")
                or payload.get("steam_status") or row["event_type"]
            ).casefold()
            category_aliases = {
                "insufficient_stock": "insufficient_stock",
                "privacy_wait": "privacy_wait",
                "trade_restricted": "trade_restricted",
                "declined": "declined",
                "canceled": "declined",
                "cancelled": "declined",
                "expired": "declined",
                "unknown": "unknown",
                "operator_support_request": "support_request",
                "new_order": "new_order",
            }
            category = category_aliases.get(raw_category, (
                "support_request" if row["event_type"] == "operator_support_request"
                else row["event_type"]
            ))
            outbox_id = self.enqueue(
                event_type=category,
                chat_id=str(operator_chat_id),
                order_id=row["order_id"],
                payload={
                    "chat_id": str(operator_chat_id),
                    "text": json.dumps({
                        "event": row["event_type"], "order_id": row["order_id"],
                        "details": payload,
                    }, ensure_ascii=False),
                    "reply_markup": None,
                },
                idempotency_key="delivery-event:" + row["event_id"],
            )
            if outbox_id is not None:
                with self.database.connect() as db:
                    db.execute(
                        "UPDATE gd_delivery_events SET status='queued' WHERE event_id=? AND status='pending'",
                        (row["event_id"],),
                    )
                queued += 1
        return queued

    def bind_operator_message(self, *, telegram_message_id: str, order_id: str,
                              operator_chat_id: str = "") -> None:
        with self.database.connect() as db:
            row = db.execute("""
                SELECT o.order_id,o.deal_id,d.chat_id
                FROM gd_orders o JOIN gd_deals d ON d.deal_id=o.deal_id
                WHERE o.order_id=?
            """, (order_id,)).fetchone()
            if row is None or not row["chat_id"]:
                raise KeyError("Заказ не имеет однозначной привязки к сделке и чату")
            db.execute("""
                INSERT INTO telegram_operator_messages
                    (telegram_message_id,order_id,deal_id,chat_id,operator_chat_id)
                VALUES (?,?,?,?,?)
                ON CONFLICT(telegram_message_id) DO UPDATE SET
                    order_id=excluded.order_id,deal_id=excluded.deal_id,chat_id=excluded.chat_id,
                    operator_chat_id=excluded.operator_chat_id
            """, (str(telegram_message_id), row["order_id"], row["deal_id"], row["chat_id"],
                  str(operator_chat_id)))


class BuyerPlatformMessageOutbox:
    """Durable queue boundary for replies that must be sent to the marketplace buyer."""

    def __init__(self, database):
        self.database = database
        with self.database.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS gd_buyer_message_outbox (
                    message_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    deal_id TEXT NOT NULL REFERENCES gd_deals(deal_id) ON DELETE RESTRICT,
                    chat_id TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)

    def enqueue(self, deal_id: str, chat_id: str, message: str) -> str:
        text = str(message).strip()
        if not text or len(text) > 4000:
            raise ValueError("Сообщение покупателю должно содержать от 1 до 4000 символов")
        with self.database.connect() as db:
            deal = db.execute(
                "SELECT chat_id FROM gd_deals WHERE deal_id=?", (deal_id,)
            ).fetchone()
            if deal is None or str(deal["chat_id"] or "") != str(chat_id):
                raise ValueError("Связка deal_id/chat_id не подтверждена")
            import hashlib
            key = hashlib.sha256((deal_id + "|" + str(chat_id) + "|" + text).encode()).hexdigest()
            message_id = "buyer-msg:" + key[:32]
            db.execute("""
                INSERT OR IGNORE INTO gd_buyer_message_outbox
                    (message_id,idempotency_key,deal_id,chat_id,message_text)
                VALUES (?,?,?,?,?)
            """, (message_id, key, deal_id, str(chat_id), text))
        return message_id


class TelegramOperatorReplyRouter:
    """Accept operator replies only from explicit user/chat allowlists."""

    def __init__(
        self, database, *, allowed_user_ids: set[str], allowed_chat_ids: set[str],
        buyer_message_enqueue: Callable[[str, str, str], Any],
    ):
        self.database = database
        self.allowed_users = {str(value) for value in allowed_user_ids}
        self.allowed_chats = {str(value) for value in allowed_chat_ids}
        self.buyer_message_enqueue = buyer_message_enqueue

    def handle_reply(
        self, *, from_user_id: str, operator_chat_id: str,
        reply_to_message_id: str, text: str,
    ) -> bool:
        if str(from_user_id) not in self.allowed_users or str(operator_chat_id) not in self.allowed_chats:
            return False
        if not str(text).strip() or len(str(text)) > 4000:
            return False
        with self.database.connect() as db:
            rows = db.execute("""
                SELECT m.order_id,m.deal_id,m.chat_id,m.operator_chat_id,d.chat_id AS current_chat
                FROM telegram_operator_messages m
                JOIN gd_orders o ON o.order_id=m.order_id AND o.deal_id=m.deal_id
                JOIN gd_deals d ON d.deal_id=m.deal_id
                WHERE m.telegram_message_id=?
            """, (str(reply_to_message_id),)).fetchall()
        if (len(rows) != 1 or rows[0]["chat_id"] != rows[0]["current_chat"]
                or rows[0]["operator_chat_id"] != str(operator_chat_id)):
            return False
        row = rows[0]
        self.buyer_message_enqueue(row["deal_id"], row["chat_id"], str(text).strip())
        return True
