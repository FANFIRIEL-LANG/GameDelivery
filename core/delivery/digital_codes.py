"""Protected stock and exactly-once lifecycle for digital codes.

The database deliberately stores only opaque identifiers and hashes. The code
itself is kept in the operating-system keyring and is returned only from an
explicit reveal call.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping


class DigitalCodeError(ValueError):
    pass


class DigitalCodeConflict(DigitalCodeError):
    pass


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean_code(value: Any) -> str:
    if not isinstance(value, str):
        raise DigitalCodeError("Код должен быть строкой")
    code = value.strip()
    if not code or len(code) > 4096 or _CONTROL.search(code):
        raise DigitalCodeError("Код пустой или имеет недопустимый формат")
    return code


def _source_lines(source: str | Path | Iterable[str]) -> list[str]:
    if isinstance(source, Path):
        if not source.is_file():
            raise DigitalCodeError("Файл импорта отсутствует")
        return source.read_text(encoding="utf-8").splitlines()
    if isinstance(source, str):
        # Treat a string as a path only when it is a short, existing file path;
        # arbitrary code text must never be passed to Path.exists().
        if len(source) <= 4096:
            candidate = Path(source)
            try:
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8").splitlines()
            except OSError:
                pass
        return source.splitlines()
    return list(source)


def preview_import(source: str | Path | Iterable[str]) -> dict[str, Any]:
    """Validate an import without writing the file, keyring, or database."""
    lines = _source_lines(source)
    valid: list[str] = []
    duplicates = 0
    invalid = 0
    seen: set[str] = set()
    for raw in lines:
        if not str(raw).strip():
            continue
        try:
            code = _clean_code(raw)
        except DigitalCodeError:
            invalid += 1
            continue
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        if digest in seen:
            duplicates += 1
            continue
        seen.add(digest)
        valid.append(code)
    return {
        "total_lines": len(lines), "valid_count": len(valid),
        "duplicate_count": duplicates, "invalid_count": invalid,
        "ready": bool(valid) and duplicates == 0 and invalid == 0,
    }


class DigitalCodeStore:
    """SQLite lifecycle with keyring-backed secret values."""

    SERVICE = "GameDelivery.DigitalCodes"

    def __init__(self, database, keyring_backend: Any):
        self.database = database
        self.keyring = keyring_backend
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self.database.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS gd_digital_codes (
                    code_id TEXT PRIMARY KEY,
                    product_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL,
                    code_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL DEFAULT 'available'
                        CHECK(state IN ('available','reserved','revealed','unknown','released','consumed')),
                    order_id TEXT,
                    order_line_id TEXT,
                    reserved_at TEXT,
                    revealed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_gd_digital_codes_stock
                    ON gd_digital_codes(product_id,state,batch_id);
                CREATE TABLE IF NOT EXISTS gd_digital_code_audit (
                    audit_id TEXT PRIMARY KEY,
                    code_id TEXT NOT NULL REFERENCES gd_digital_codes(code_id),
                    action TEXT NOT NULL,
                    order_id TEXT,
                    actor TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_gd_digital_code_audit_code
                    ON gd_digital_code_audit(code_id,created_at);
            """)
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(gd_digital_codes)")}
            if "order_line_id" not in columns:
                db.execute("ALTER TABLE gd_digital_codes ADD COLUMN order_line_id TEXT")

    @classmethod
    def _key(cls, code_id: str) -> str:
        return code_id

    @staticmethod
    def _id(code: str) -> tuple[str, str]:
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        return "code:" + digest, digest

    def import_codes(self, *, product_id: str, batch_id: str, source: str | Path | Iterable[str], actor: str = "operator") -> dict[str, Any]:
        product_id, batch_id = product_id.strip(), batch_id.strip()
        if not product_id or not batch_id:
            raise DigitalCodeError("product_id и batch_id обязательны")
        report = preview_import(source)
        if not report["ready"]:
            raise DigitalCodeError("Импорт не прошёл preview-проверку")
        raw_lines = _source_lines(source)
        codes = [_clean_code(line) for line in raw_lines if str(line).strip()]
        prepared = [(*self._id(code), code) for code in codes]
        stored: list[str] = []
        try:
            with self.database.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                for code_id, digest, code in prepared:
                    existing = db.execute("SELECT 1 FROM gd_digital_codes WHERE code_hash=?", (digest,)).fetchone()
                    if existing:
                        raise DigitalCodeConflict("Код уже импортирован")
                    self.keyring.set_password(self.SERVICE, self._key(code_id), code)
                    stored.append(code_id)
                    db.execute("INSERT INTO gd_digital_codes(code_id,product_id,batch_id,code_hash) VALUES(?,?,?,?)",
                               (code_id, product_id, batch_id, digest))
                    db.execute("INSERT INTO gd_digital_code_audit(audit_id,code_id,action,actor) VALUES(?,?,?,?)",
                               (str(uuid.uuid4()), code_id, "imported", actor))
                db.commit()
        except Exception:
            for code_id in stored:
                try:
                    self.keyring.delete_password(self.SERVICE, self._key(code_id))
                except Exception:
                    pass
            raise
        return {"imported": len(stored), "product_id": product_id, "batch_id": batch_id}

    @staticmethod
    def reserve_for_order_line(connection, *, product_id: str, order_id: str, order_line_id: str, quantity: int, actor: str = "system") -> tuple[str, ...]:
        """Reserve exact unique codes inside an existing BEGIN IMMEDIATE transaction."""
        product_id, order_id, order_line_id = product_id.strip(), order_id.strip(), order_line_id.strip()
        if not product_id or not order_id or not order_line_id:
            raise DigitalCodeError("product_id, order_id и order_line_id обязательны")
        if type(quantity) is not int or quantity < 1:
            raise DigitalCodeError("quantity должен быть положительным целым")
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(gd_digital_codes)")}
        if "order_line_id" not in columns:
            connection.execute("ALTER TABLE gd_digital_codes ADD COLUMN order_line_id TEXT")
        existing = connection.execute(
            """SELECT code_id FROM gd_digital_codes
               WHERE order_id=? AND order_line_id=?
                 AND state IN ('reserved','revealed','unknown','consumed')
               ORDER BY created_at,code_id""",
            (order_id, order_line_id),
        ).fetchall()
        if existing:
            if len(existing) != quantity:
                raise DigitalCodeConflict("Для order line уже существует другой резерв кодов")
            return tuple(str(row["code_id"]) for row in existing)
        rows = connection.execute(
            "SELECT code_id FROM gd_digital_codes WHERE product_id=? AND state='available' ORDER BY created_at,code_id LIMIT ?",
            (product_id, quantity),
        ).fetchall()
        if len(rows) != quantity:
            raise DigitalCodeError("Недостаточно доступных уникальных кодов")
        code_ids = tuple(str(row["code_id"]) for row in rows)
        for code_id in code_ids:
            changed = connection.execute(
                "UPDATE gd_digital_codes SET state='reserved',order_id=?,order_line_id=?,reserved_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE code_id=? AND state='available'",
                (order_id, order_line_id, code_id),
            ).rowcount
            if changed != 1:
                raise DigitalCodeConflict("Код уже зарезервирован")
            connection.execute(
                "INSERT INTO gd_digital_code_audit(audit_id,code_id,action,order_id,actor,evidence_json) VALUES(?,?,?,?,?,?)",
                (str(uuid.uuid4()), code_id, "reserved", order_id, actor, __import__('json').dumps({"order_line_id": order_line_id}, sort_keys=True)),
            )
        return code_ids

    def list_codes(self, *, product_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise DigitalCodeError("limit должен быть от 1 до 1000")
        sql = "SELECT code_id,product_id,batch_id,state,order_id,reserved_at,revealed_at FROM gd_digital_codes"
        params: tuple[Any, ...] = ()
        if product_id is not None and product_id.strip():
            sql += " WHERE product_id=?"; params = (product_id.strip(),)
        sql += " ORDER BY created_at DESC LIMIT ?"; params += (limit,)
        with self.database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def reserve(self, *, product_id: str, order_id: str, actor: str = "system") -> dict[str, Any]:
        product_id, order_id = product_id.strip(), order_id.strip()
        if not product_id or not order_id:
            raise DigitalCodeError("product_id и order_id обязательны")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM gd_digital_codes WHERE product_id=? AND state='available' ORDER BY created_at,code_id LIMIT 1", (product_id,)).fetchone()
            if row is None:
                raise DigitalCodeError("Для товара нет доступного кода")
            db.execute("UPDATE gd_digital_codes SET state='reserved',order_id=?,reserved_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE code_id=? AND state='available'", (order_id, row["code_id"]))
            db.execute("INSERT INTO gd_digital_code_audit(audit_id,code_id,action,order_id,actor) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), row["code_id"], "reserved", order_id, actor))
            db.commit()
            return {"code_id": row["code_id"], "product_id": product_id, "order_id": order_id, "state": "reserved"}

    def reveal(self, *, code_id: str, order_id: str, actor: str = "operator") -> str:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,order_id FROM gd_digital_codes WHERE code_id=?", (code_id,)).fetchone()
            if row is None or row["order_id"] != order_id:
                raise DigitalCodeError("Код не закреплён за указанным заказом")
            if row["state"] != "reserved":
                raise DigitalCodeError("Код нельзя раскрыть в текущем состоянии")
            value = self.keyring.get_password(self.SERVICE, self._key(code_id))
            if not value:
                raise DigitalCodeError("Секрет кода отсутствует в keyring")
            db.execute("UPDATE gd_digital_codes SET state='revealed',revealed_at=COALESCE(revealed_at,strftime('%Y-%m-%dT%H:%M:%fZ','now')),updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE code_id=?", (code_id,))
            db.execute("INSERT INTO gd_digital_code_audit(audit_id,code_id,action,order_id,actor) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), code_id, "revealed", order_id, actor))
            db.commit()
            return value

    def mark_unknown(self, *, code_id: str, order_id: str, actor: str = "system", evidence: Mapping[str, Any] | None = None) -> None:
        self._transition(code_id, order_id, "unknown", "unknown", actor, evidence)

    def reconcile(self, *, code_id: str, order_id: str, outcome: str, actor: str, evidence: Mapping[str, Any] | None = None) -> None:
        if outcome not in {"consumed", "released"}:
            raise DigitalCodeError("reconcile outcome должен быть consumed или released")
        self._transition(code_id, order_id, outcome, "reconciled_" + outcome, actor, evidence)

    def _transition(self, code_id: str, order_id: str, state: str, action: str, actor: str, evidence: Mapping[str, Any] | None) -> None:
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,order_id FROM gd_digital_codes WHERE code_id=?", (code_id,)).fetchone()
            if row is None or row["order_id"] != order_id or row["state"] not in {"reserved", "revealed", "unknown"}:
                raise DigitalCodeError("Невозможно изменить состояние кода")
            if action.startswith("reconciled_") and row["state"] != "unknown":
                raise DigitalCodeError("Только UNKNOWN требует явного reconcile")
            db.execute("UPDATE gd_digital_codes SET state=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE code_id=?", (state, code_id))
            db.execute("INSERT INTO gd_digital_code_audit(audit_id,code_id,action,order_id,actor,evidence_json) VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), code_id, action, order_id, actor, __import__('json').dumps(dict(evidence or {}), sort_keys=True)))
