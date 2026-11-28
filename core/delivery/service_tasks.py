from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping


class ServiceTaskError(ValueError):
    pass


class ServiceTaskStore:
    """Manual service units with explicit evidence and immutable audit rows."""

    STATES = {"reserved", "in_progress", "awaiting_confirmation", "completed", "cancelled", "disputed"}
    TRANSITIONS = {
        "reserved": {"in_progress", "cancelled", "disputed"},
        "in_progress": {"in_progress", "awaiting_confirmation", "cancelled", "disputed"},
        "awaiting_confirmation": {"completed", "in_progress", "disputed", "cancelled"},
        "disputed": {"in_progress", "cancelled", "completed"},
        "completed": set(), "cancelled": set(),
    }

    def __init__(self, database):
        self.database = database
        with self.database.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS gd_service_units (
                    unit_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, order_id TEXT, quantity INTEGER NOT NULL CHECK(quantity > 0),
                    instructions TEXT NOT NULL, criteria TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'reserved',
                    completed_units INTEGER NOT NULL DEFAULT 0 CHECK(completed_units >= 0 AND completed_units <= quantity),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS gd_service_audit (
                    audit_id TEXT PRIMARY KEY, unit_id TEXT NOT NULL REFERENCES gd_service_units(unit_id), action TEXT NOT NULL,
                    actor TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
            """)

    def reserve(self, *, product_id: str, order_id: str, quantity: int, instructions: str, criteria: str) -> str:
        if not product_id.strip() or not order_id.strip() or not isinstance(quantity, int) or quantity < 1 or not instructions.strip() or not criteria.strip():
            raise ServiceTaskError("Товар, order_id, количество, инструкции и критерии обязательны")
        product_id, order_id = product_id.strip(), order_id.strip()
        unit_id = "service:" + str(uuid.uuid4())
        with self.database.connect() as db:
            db.execute("INSERT INTO gd_service_units(unit_id,product_id,order_id,quantity,instructions,criteria) VALUES(?,?,?,?,?,?)", (unit_id,product_id,order_id,quantity,instructions,criteria))
            self._audit(db, unit_id, "reserved", "system", {})
        return unit_id

    def transition(self, *, unit_id: str, state: str, actor: str, evidence: Mapping[str, Any] | None = None, completed_units: int | None = None) -> None:
        if state not in self.STATES or not actor.strip():
            raise ServiceTaskError("Недопустимое состояние или actor")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM gd_service_units WHERE unit_id=?", (unit_id,)).fetchone()
            if row is None:
                raise ServiceTaskError("Единица услуги не найдена")
            if state != row["state"] and state not in self.TRANSITIONS.get(row["state"], set()):
                raise ServiceTaskError("Недопустимый переход состояния услуги")
            done = row["completed_units"] if completed_units is None else completed_units
            if not isinstance(done, int) or done < 0 or done > row["quantity"]:
                raise ServiceTaskError("Неверное число выполненных единиц")
            evidence_data = dict(evidence or {})
            if state in {"awaiting_confirmation", "completed"} and not any(
                str(value).strip() for value in evidence_data.values() if value is not None
            ):
                raise ServiceTaskError("Для подтверждения услуги требуется evidence")
            if state == "completed" and done != row["quantity"]:
                raise ServiceTaskError("Нельзя завершить услугу без выполнения всех единиц")
            db.execute("UPDATE gd_service_units SET state=?,completed_units=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE unit_id=?", (state,done,unit_id))
            self._audit(db, unit_id, state, actor, evidence or {})

    @staticmethod
    def _audit(db, unit_id: str, action: str, actor: str, evidence: Mapping[str, Any]):
        # SQLite's strftime('%f') default has millisecond precision; several
        # service transitions can occur within one millisecond. Persist a
        # microsecond timestamp explicitly so audit readers can identify the
        # latest evidence deterministically by created_at.
        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        db.execute(
            "INSERT INTO gd_service_audit(audit_id,unit_id,action,actor,evidence_json,created_at) VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), unit_id, action, actor, json.dumps(dict(evidence), sort_keys=True), created_at),
        )
