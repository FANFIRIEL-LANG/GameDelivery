from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from integrations.playerok.browser_sales import (
    BrowserSalesSnapshot,
    is_trusted_browser_sales_snapshot,
)


class PlayerokDealSyncService:
    """Historical baseline and idempotent Playerok status synchronization."""

    ACTIVE_STATUSES = ("PAID", "SENT", "CONFIRMED", "CONFIRMED_AUTOMATICALLY")

    def __init__(self, database: Any, *, send_policy: Any = None):
        self.database = database
        self.send_policy = send_policy
        database.initialize()
        with database.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS playerok_sync_state (
                    account_user_id TEXT PRIMARY KEY,
                    baseline_completed INTEGER NOT NULL DEFAULT 0 CHECK (baseline_completed IN (0,1)),
                    baseline_completed_at TEXT,
                    last_sync_at TEXT
                );
                CREATE TABLE IF NOT EXISTS playerok_status_outbox (
                    idempotency_key TEXT PRIMARY KEY,
                    account_user_id TEXT NOT NULL,
                    order_id TEXT NOT NULL UNIQUE REFERENCES gd_orders(order_id) ON DELETE RESTRICT,
                    deal_id TEXT NOT NULL UNIQUE REFERENCES gd_deals(deal_id) ON DELETE RESTRICT,
                    external_deal_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','dispatching','unknown','sent','confirmed','blocked')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_type TEXT NOT NULL DEFAULT '',
                    last_platform_status TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    sent_at TEXT,
                    confirmed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_playerok_outbox_poll
                    ON playerok_status_outbox(account_user_id,state,updated_at);
                DROP TRIGGER IF EXISTS gd_orders_playerok_delivered;
                CREATE TRIGGER gd_orders_playerok_delivered
                AFTER UPDATE OF status ON gd_orders
                WHEN NEW.status='DELIVERED' AND OLD.status<>'DELIVERED'
                BEGIN
                    INSERT OR IGNORE INTO playerok_status_outbox
                        (idempotency_key,account_user_id,order_id,deal_id,external_deal_id)
                    SELECT 'playerok-send:' || NEW.order_id,d.playerok_account_id,
                           NEW.order_id,d.deal_id,d.external_deal_id
                    FROM gd_deals d
                    WHERE d.deal_id=NEW.deal_id
                      AND d.source_platform='playerok'
                      AND COALESCE(d.is_historical,0)=0
                      AND d.playerok_account_id IS NOT NULL
                      AND d.playerok_account_id<>'';
                END;
            """)
            # A crash during a remote call is ambiguous; never resend such mutations.
            db.execute("""
                UPDATE playerok_status_outbox SET state='unknown',
                    last_error_type=CASE WHEN last_error_type='' THEN 'InterruptedDispatch' ELSE last_error_type END,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE state='dispatching'
            """)

    @staticmethod
    def _stable(prefix: str, value: str) -> str:
        return prefix + ":" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _status(deal: dict[str, Any]) -> str:
        return str(deal.get("status") or "UNKNOWN").strip().upper()

    def _listing_config(self, db: sqlite3.Connection, deal: dict[str, Any]):
        external_listing_id = str(deal.get("listing_id") or "").strip()
        if not external_listing_id:
            return None, None, None
        row = db.execute(
            "SELECT listing_id FROM gd_listings WHERE source_platform='playerok' AND external_listing_id=?",
            (external_listing_id,),
        ).fetchone()
        if row:
            listing_id = str(row["listing_id"])
            db.execute(
                "UPDATE gd_listings SET title=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE listing_id=?",
                (str(deal.get("title") or ""), listing_id),
            )
        else:
            listing_id = self._stable("playerok-listing", external_listing_id)
            db.execute("""
                INSERT INTO gd_listings(listing_id,source_platform,external_listing_id,title,status)
                VALUES(?,'playerok',?,?,'unconfigured')
            """, (listing_id, external_listing_id, str(deal.get("title") or "")))
        config = db.execute("""
            SELECT config_version_id,delivery_quantity_per_deal,item_selectors_json,bot_pool_json
            FROM gd_config_versions WHERE listing_id=? ORDER BY version_number DESC LIMIT 1
        """, (listing_id,)).fetchone()
        if config is None:
            config_id = listing_id + ":config:1"
            selector = {"item_type": "playerok_unconfigured", "external_listing_id": external_listing_id}
            db.execute("""
                INSERT INTO gd_config_versions
                    (config_version_id,listing_id,version_number,delivery_quantity_per_deal,item_selectors_json,bot_pool_json)
                VALUES(?,?,1,NULL,?,'[]')
            """, (config_id, listing_id, self._json(selector)))
            config = {
                "config_version_id": config_id,
                "delivery_quantity_per_deal": None,
                "item_selectors_json": self._json(selector),
                "bot_pool_json": "[]",
            }
        return listing_id, str(config["config_version_id"]), dict(config)

    def _upsert_deal(self, db: sqlite3.Connection, deal: dict[str, Any], account_user_id: str, historical: bool):
        account_id = str(account_user_id or "").strip()
        external_id = str(deal.get("deal_id") or "").strip()
        if not account_id:
            raise ValueError("Playerok account_user_id обязателен")
        if not external_id:
            raise ValueError("Playerok deal_id обязателен")
        source_listing_ref = str(
            deal.get("source_listing_ref") or deal.get("listing_reference") or ""
        ).strip()
        listing_id, config_id, config = self._listing_config(db, deal)
        old = db.execute(
            "SELECT deal_id,is_historical,listing_id,config_version_id,listing_resolution_status "
            "FROM gd_deals WHERE source_platform='playerok' AND playerok_account_id=? "
            "AND external_deal_id=?",
            (account_id, external_id),
        ).fetchone()
        created = old is None
        normalized_id = str(old["deal_id"]) if old else self._stable(
            "playerok-deal", account_id + "\x1f" + external_id
        )
        historical_value = int(historical or (bool(old["is_historical"]) if old else False))
        if old is not None:
            previous_listing = str(old["listing_id"] or "")
            if previous_listing and listing_id and previous_listing != listing_id:
                raise ValueError("Playerok deal получил конфликтующую связь с объявлением")
            if previous_listing and listing_id is None:
                listing_id = previous_listing
                config_id = str(old["config_version_id"]) if old["config_version_id"] else None
                config_row = db.execute(
                    "SELECT config_version_id,delivery_quantity_per_deal,item_selectors_json,bot_pool_json "
                    "FROM gd_config_versions WHERE config_version_id=?", (config_id,),
                ).fetchone() if config_id else None
                config = dict(config_row) if config_row else None
        listing_resolution_status = "resolved" if listing_id else "unresolved"
        if not listing_id:
            config_id = None
            config = None
        # Freeze the ConfigVersion once an order exists: listing edits are only
        # applicable to future purchases. Deals awaiting setup may adopt a new version.
        if old is not None:
            existing_order = db.execute(
                "SELECT 1 FROM gd_orders WHERE deal_id=? LIMIT 1", (normalized_id,)
            ).fetchone()
            if existing_order is not None:
                listing_id = str(old["listing_id"])
                config_id = str(old["config_version_id"])
                frozen_config = db.execute(
                    "SELECT config_version_id,delivery_quantity_per_deal,item_selectors_json,bot_pool_json "
                    "FROM gd_config_versions WHERE config_version_id=?",
                    (config_id,),
                ).fetchone()
                if frozen_config is None:
                    raise RuntimeError("ConfigVersion сохранённой сделки не найден")
                config = dict(frozen_config)
        payload = {
            "buyer": str(deal.get("buyer") or ""),
            "buyer_id": str(deal.get("buyer_id") or ""),
            "title": str(deal.get("title") or ""),
            "amount": str(deal.get("amount") or ""),
            "created_at": str(deal.get("created_at") or ""),
            "deal_url": str(deal.get("deal_url") or deal.get("url") or ""),
            "chat_url": str(deal.get("chat_url") or ""),
        }
        db.execute("""
            INSERT INTO gd_deals
                (deal_id,source_platform,external_deal_id,chat_id,listing_id,config_version_id,
                 platform_status,payload_json,playerok_account_id,is_historical,
                 source_listing_ref,listing_resolution_status)
            VALUES(?,'playerok',?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(deal_id) DO UPDATE SET
                chat_id=excluded.chat_id,listing_id=excluded.listing_id,
                config_version_id=excluded.config_version_id,
                platform_status=excluded.platform_status,payload_json=excluded.payload_json,
                playerok_account_id=excluded.playerok_account_id,
                is_historical=CASE WHEN gd_deals.is_historical=1 THEN 1 ELSE excluded.is_historical END,
                source_listing_ref=CASE WHEN excluded.source_listing_ref=''
                    THEN gd_deals.source_listing_ref ELSE excluded.source_listing_ref END,
                listing_resolution_status=excluded.listing_resolution_status
        """, (
            normalized_id, external_id, str(deal.get("chat_id") or "").strip() or None,
            listing_id, config_id, self._status(deal), self._json(payload),
            account_id, historical_value, source_listing_ref, listing_resolution_status,
        ))

        if config is not None:
            self._maybe_create_order(
                db, normalized_id, external_id, account_id, self._status(deal),
                historical_value, listing_id, config_id, config,
            )
        return normalized_id, created

    def resolve_unresolved_deal(
        self, account_user_id: str, external_deal_id: str, external_listing_id: str
    ) -> dict[str, Any]:
        """Attach an existing deal to an exact, already-known Playerok listing ID."""
        account_id = str(account_user_id or "").strip()
        deal_id = str(external_deal_id or "").strip()
        listing_external_id = str(external_listing_id or "").strip()
        if not account_id or not deal_id or not listing_external_id:
            raise ValueError("Account ID, deal_id и точный listing_id обязательны")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT deal_id,is_historical,platform_status,listing_id,listing_resolution_status "
                "FROM gd_deals WHERE source_platform='playerok' AND playerok_account_id=? "
                "AND external_deal_id=?",
                (account_id, deal_id),
            ).fetchone()
            if row is None:
                raise KeyError("Playerok deal не найден для этого аккаунта")
            listing = db.execute(
                "SELECT listing_id FROM gd_listings WHERE source_platform='playerok' "
                "AND external_listing_id=?", (listing_external_id,),
            ).fetchone()
            if listing is None:
                raise KeyError("Точный listing_id ещё не зарегистрирован в ConfigVersion")
            listing_id = str(listing["listing_id"])
            config = db.execute(
                "SELECT config_version_id,delivery_quantity_per_deal,item_selectors_json,bot_pool_json "
                "FROM gd_config_versions WHERE listing_id=? ORDER BY version_number DESC LIMIT 1",
                (listing_id,),
            ).fetchone()
            if row["listing_id"] and str(row["listing_id"]) != listing_id:
                raise ValueError("Сделка уже сопоставлена с другим объявлением")
            config_id = str(config["config_version_id"]) if config else None
            db.execute(
                "UPDATE gd_deals SET listing_id=?,config_version_id=?,listing_resolution_status='resolved', "
                "source_listing_ref='' WHERE deal_id=? AND playerok_account_id=?",
                (listing_id, config_id, row["deal_id"], account_id),
            )
            if config:
                self._maybe_create_order(
                    db, str(row["deal_id"]), deal_id, account_id,
                    str(row["platform_status"]), int(row["is_historical"]), listing_id,
                    config_id, dict(config),
                )
        return {"deal_id": deal_id, "listing_id": listing_external_id, "resolved": True}

    def _maybe_create_order(
        self, db, normalized_id, external_id, account_id, status, historical,
        listing_id, config_id, config,
    ) -> None:
        baseline = db.execute(
            "SELECT baseline_completed FROM playerok_sync_state WHERE account_user_id=?",
            (account_id,),
        ).fetchone()
        quantity = config.get("delivery_quantity_per_deal")
        try:
            bot_pool = json.loads(config.get("bot_pool_json") or "[]")
        except (TypeError, ValueError):
            bot_pool = []
        if (
            not historical and baseline is not None and baseline[0] == 1
            and listing_id is not None and config_id is not None and status == "PAID"
            and type(quantity) is int and quantity > 0 and isinstance(bot_pool, list)
            and any(isinstance(bot, str) and bot.strip() for bot in bot_pool)
        ):
            order_id = self._stable("playerok-order", account_id + "\x1f" + external_id)
            db.execute(
                "INSERT OR IGNORE INTO gd_orders(order_id,deal_id,status,total_quantity) "
                "VALUES(?,?,'pending',?)", (order_id, normalized_id, quantity),
            )

    def _baseline(
        self, adapter: Any, account_user_id: str, *, allow_status_mutation: bool = False
    ) -> dict[str, Any]:
        # Do not commit a partial scan as a completed baseline.
        deals = adapter.fetch_sales(statuses=None)
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for deal in deals:
                self._upsert_deal(db, deal, account_user_id, historical=True)
            db.execute("""
                INSERT INTO playerok_sync_state
                    (account_user_id,baseline_completed,baseline_completed_at,last_sync_at)
                VALUES(?,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(account_user_id) DO UPDATE SET
                    baseline_completed=1,
                    baseline_completed_at=COALESCE(playerok_sync_state.baseline_completed_at,excluded.baseline_completed_at),
                    last_sync_at=excluded.last_sync_at
            """, (account_user_id,))
        return {
            "baseline": True, "baseline_deals": len(deals), "deals": deals,
            "new_deals": 0, "historical_deals": len(deals),
            "status_updates": self.process_status_outbox(
                adapter, account_user_id, allow_status_mutation=allow_status_mutation
            ),
        }

    def _upsert_incremental(self, deals: list[dict[str, Any]], account_user_id: str) -> int:
        created = 0
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for deal in deals:
                _normalized_id, is_new = self._upsert_deal(db, deal, account_user_id, historical=False)
                created += int(is_new)
            db.execute("""
                UPDATE playerok_sync_state SET last_sync_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE account_user_id=?
            """, (account_user_id,))
        return created

    def _set_outbox(self, key: str, state: str, *, error_type: str = "", platform_status: str = ""):
        with self.database.connect() as db:
            db.execute("""
                UPDATE playerok_status_outbox
                SET state=?,last_error_type=?,
                    last_platform_status=CASE WHEN ?='' THEN last_platform_status ELSE ? END,
                    sent_at=CASE WHEN ? IN ('sent','confirmed') THEN COALESCE(sent_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) ELSE sent_at END,
                    confirmed_at=CASE WHEN ?='confirmed' THEN COALESCE(confirmed_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')) ELSE confirmed_at END,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE idempotency_key=?
            """, (state,error_type[:80],platform_status,platform_status,state,state,key))

    def process_status_outbox(
        self, adapter: Any, account_user_id: str, *, allow_status_mutation: bool = False
    ) -> int:
        with self.database.connect() as db:
            rows = [dict(row) for row in db.execute("""
                SELECT * FROM playerok_status_outbox
                WHERE account_user_id=? AND state IN ('pending','unknown','sent')
                ORDER BY created_at,idempotency_key
            """, (account_user_id,)).fetchall()]
        changed_count = 0
        for row in rows:
            key = row["idempotency_key"]
            try:
                live = adapter.fetch_deal(row["external_deal_id"])
            except Exception as exc:
                self._set_outbox(key, "unknown", error_type=type(exc).__name__)
                continue
            live_status = self._status(live)
            if live_status == "PAID":
                # Persist the observed platform state while normal sync remains read-only.
                self._set_outbox(key, row["state"], platform_status=live_status)
            with self.database.connect() as db:
                db.execute(
                    "UPDATE gd_deals SET platform_status=?,chat_id=? WHERE deal_id=?",
                    (live_status, str(live.get("chat_id") or "").strip() or None, row["deal_id"]),
                )
            if live_status in {"CONFIRMED","CONFIRMED_AUTOMATICALLY"}:
                if row["state"] != "confirmed":
                    self._set_outbox(key, "confirmed", platform_status=live_status)
                    changed_count += 1
                continue
            if live_status == "SENT":
                if row["state"] != "sent":
                    self._set_outbox(key, "sent", platform_status=live_status)
                    changed_count += 1
                continue
            if row["state"] in {"unknown","sent"}:
                # Uncertain or already-sent deals are observed only; no blind mutation retry.
                continue
            if live_status != "PAID":
                self._set_outbox(key, "blocked", error_type="UnexpectedDealStatus", platform_status=live_status)
                changed_count += 1
                continue
            # Normal application sync is read-only. The embedded Playerok browser
            # introduced in Part B owns the "Выполнил заказ" UI action.
            claim_auto_dispatch = getattr(self.send_policy, "run_auto_dispatch_claim", None)
            if not allow_status_mutation or not callable(claim_auto_dispatch):
                continue

            def claim_status_mutation():
                with self.database.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    claimed = db.execute("""
                        UPDATE playerok_status_outbox
                        SET state='dispatching',attempt_count=attempt_count+1,last_error_type='',
                            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                        WHERE idempotency_key=? AND state='pending'
                    """, (key,)).rowcount
                return {"_claimed": claimed == 1}

            authorized, claim = claim_auto_dispatch(claim_status_mutation)
            if not authorized or not isinstance(claim, dict) or claim.get("_claimed") is not True:
                continue
            try:
                updated = adapter.update_deal_status(row["external_deal_id"], "SENT")
            except Exception as exc:
                self._set_outbox(key, "unknown", error_type=type(exc).__name__)
                continue
            result_status = self._status(updated)
            if result_status in {"CONFIRMED","CONFIRMED_AUTOMATICALLY"}:
                self._set_outbox(key, "confirmed", platform_status=result_status)
                changed_count += 1
            elif result_status == "SENT":
                self._set_outbox(key, "sent", platform_status=result_status)
                changed_count += 1
            else:
                self._set_outbox(key, "unknown", error_type="MutationNotConfirmed", platform_status=result_status)
        return changed_count

    def sync(self, adapter: Any, *, allow_status_mutation: bool = False) -> dict[str, Any]:
        account_user_id = str(getattr(adapter, "account_user_id", "") or "").strip()
        if not account_user_id:
            raise ValueError("Сначала проверь подключённую сессию Playerok.")
        with self.database.connect() as db:
            state = db.execute(
                "SELECT baseline_completed FROM playerok_sync_state WHERE account_user_id=?",
                (account_user_id,),
            ).fetchone()
        if state is None or state["baseline_completed"] != 1:
            return self._baseline(adapter, account_user_id, allow_status_mutation=allow_status_mutation)

        deals = adapter.fetch_sales(statuses=self.ACTIVE_STATUSES)
        new_count = self._upsert_incremental(deals, account_user_id)
        outbox_updates = self.process_status_outbox(
            adapter, account_user_id, allow_status_mutation=allow_status_mutation
        )
        return {
            "baseline": False, "baseline_deals": 0, "deals": deals,
            "new_deals": new_count, "historical_deals": 0,
            "status_updates": outbox_updates,
        }

    def sync_browser_snapshot(self, snapshot: BrowserSalesSnapshot) -> dict[str, Any]:
        """Persist only a complete snapshot registered by BrowserSalesReader."""
        if not isinstance(snapshot, BrowserSalesSnapshot) or not is_trusted_browser_sales_snapshot(snapshot):
            raise ValueError("Playerok snapshot is incomplete or lacks trusted reader provenance.")
        account_id = str(snapshot.account_user_id or "").strip()
        deals = snapshot.deals
        if not account_id or not isinstance(deals, list):
            raise ValueError("Browser snapshot has an invalid account or deal list.")
        seen: set[str] = set()
        for deal in deals:
            if not isinstance(deal, dict):
                raise ValueError("Browser snapshot has an invalid deal entry.")
            external_id = str(deal.get("deal_id") or "").strip()
            if not external_id or external_id in seen:
                raise ValueError("Browser snapshot has a missing or duplicate deal_id.")
            seen.add(external_id)
        with self.database.connect() as db:
            state = db.execute(
                "SELECT baseline_completed FROM playerok_sync_state WHERE account_user_id=?",
                (account_id,),
            ).fetchone()
        if state is None or state["baseline_completed"] != 1:
            with self.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for deal in deals:
                    self._upsert_deal(db, deal, account_id, historical=True)
                db.execute("""
                    INSERT INTO playerok_sync_state
                        (account_user_id,baseline_completed,baseline_completed_at,last_sync_at)
                    VALUES(?,1,strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    ON CONFLICT(account_user_id) DO UPDATE SET
                        baseline_completed=1,
                        baseline_completed_at=COALESCE(playerok_sync_state.baseline_completed_at,excluded.baseline_completed_at),
                        last_sync_at=excluded.last_sync_at
                """, (account_id,))
            return {
                "baseline": True, "baseline_deals": len(deals), "deals": deals,
                "new_deals": 0, "historical_deals": len(deals), "status_updates": 0,
            }
        new_count = self._upsert_incremental(deals, account_id)
        return {
            "baseline": False, "baseline_deals": 0, "deals": deals,
            "new_deals": new_count, "historical_deals": 0, "status_updates": 0,
        }
