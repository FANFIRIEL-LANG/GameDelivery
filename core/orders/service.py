from __future__ import annotations

import json
import sqlite3
from typing import Any

from core.database.db import Database


STATUSES = {"queued", "processing", "completed", "failed", "cancelled"}
TRANSITIONS = {
    "queued": {"processing", "cancelled"},
    "processing": {"completed", "failed", "queued", "cancelled"},
    "failed": {"queued", "cancelled"},
    "completed": set(),
    "cancelled": set(),
}


class OrderService:
    """Persistent order queue with duplicate protection and explicit transitions."""

    def __init__(self, database: Database):
        self.database = database
        self.database.initialize()

    @staticmethod
    def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def enqueue(
        self,
        *,
        source: str,
        external_id: str,
        item_type: str,
        item_name: str,
        quantity: int = 1,
        payload: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        values = (source.strip(), external_id.strip(), item_type.strip(), item_name.strip())
        if not all(values):
            raise ValueError("source, external_id, item_type и item_name обязательны")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise ValueError("quantity должен быть целым числом больше нуля")

        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO orders (source, external_id, item_type, item_name, quantity, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, external_id) DO NOTHING
                """,
                (*values, quantity, payload_json),
            )
            created = cursor.rowcount == 1
            row = connection.execute(
                "SELECT * FROM orders WHERE source = ? AND external_id = ?",
                values[:2],
            ).fetchone()
        order = self._as_dict(row)
        if order is None:
            raise RuntimeError("Не удалось прочитать заказ после записи")
        return order, created

    def get(self, order_id: int) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._as_dict(row)

    def list_orders(self, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in STATUSES:
            raise ValueError(f"Неизвестный статус: {status}")
        with self.database.connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM orders ORDER BY id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM orders WHERE status = ? ORDER BY id", (status,)
                ).fetchall()
        return [self._as_dict(row) for row in rows if row is not None]

    def transition(
        self, order_id: int, new_status: str, error_message: str | None = None
    ) -> dict[str, Any]:
        if new_status not in STATUSES:
            raise ValueError(f"Неизвестный статус: {new_status}")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Заказ {order_id} не найден")
            current = row["status"]
            if new_status == current:
                return self._as_dict(row)  # type: ignore[return-value]
            if new_status not in TRANSITIONS[current]:
                raise ValueError(f"Недопустимый переход статуса: {current} -> {new_status}")
            connection.execute(
                """
                UPDATE orders
                SET status = ?, error_message = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                WHERE id = ?
                """,
                (new_status, error_message if new_status == "failed" else None, order_id),
            )
            updated = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
        result = self._as_dict(updated)
        if result is None:
            raise RuntimeError("Заказ исчез после изменения статуса")
        return result

    def next_queued(self) -> dict[str, Any] | None:
        """Return the oldest queued order without changing its status."""
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
        return self._as_dict(row)
