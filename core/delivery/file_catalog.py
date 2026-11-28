from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any


class FileCatalogError(ValueError):
    pass


class FileCatalog:
    """Managed file catalog; paths never escape the configured root."""

    def __init__(self, database, root: str | Path):
        self.database = database
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self):
        with self.database.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS gd_files (
                    file_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, order_id TEXT, relative_path TEXT NOT NULL UNIQUE,
                    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0), sha256 TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'available' CHECK(state IN ('available','reserved','delivered','invalid','manual_required')),
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_gd_files_product_state ON gd_files(product_id,state);
            """)
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(gd_files)")}
            if "order_id" not in columns:
                db.execute("ALTER TABLE gd_files ADD COLUMN order_id TEXT")

    def _inside(self, path: str | Path) -> Path:
        candidate = Path(path).expanduser().resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise FileCatalogError("Файл находится вне управляемого каталога") from exc
        return candidate

    def register(self, *, product_id: str, path: str | Path) -> dict[str, Any]:
        product_id = product_id.strip()
        if not product_id:
            raise FileCatalogError("product_id обязателен")
        file_path = self._inside(path)
        if not file_path.is_file():
            raise FileCatalogError("Файл отсутствует")
        data = file_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        relative = file_path.relative_to(self.root).as_posix()
        file_id = "file:" + hashlib.sha256((relative + "\0" + digest).encode()).hexdigest()
        with self.database.connect() as db:
            db.execute("""INSERT INTO gd_files(file_id,product_id,relative_path,size_bytes,sha256) VALUES(?,?,?,?,?)
                ON CONFLICT(relative_path) DO UPDATE SET product_id=excluded.product_id,size_bytes=excluded.size_bytes,sha256=excluded.sha256,state='available',updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""", (file_id,product_id,relative,len(data),digest))
        return {"file_id": file_id, "product_id": product_id, "size_bytes": len(data), "sha256": digest}

    def verify(self, file_id: str) -> dict[str, Any]:
        with self.database.connect() as db:
            row = db.execute("SELECT * FROM gd_files WHERE file_id=?", (file_id,)).fetchone()
        if row is None:
            raise FileCatalogError("Файл не найден")
        path = self._inside(self.root / row["relative_path"])
        if not path.is_file():
            return {"file_id": file_id, "valid": False, "reason": "missing"}
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        valid = len(data) == row["size_bytes"] and digest == row["sha256"]
        return {"file_id": file_id, "valid": valid, "size_bytes": len(data), "sha256": digest}

    def reserve(self, *, file_id: str, order_id: str) -> dict[str, Any]:
        order_id = order_id.strip()
        if not order_id:
            raise FileCatalogError("order_id is required")
        check = self.verify(file_id)
        if not check["valid"]:
            raise FileCatalogError("Целостность файла не подтверждена")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM gd_files WHERE file_id=?", (file_id,)).fetchone()
            if row is None or row["state"] != "available":
                raise FileCatalogError("Файл уже занят или недоступен")
            path = self._inside(self.root / row["relative_path"])
            if not path.is_file():
                raise FileCatalogError("Файл отсутствует")
            data = path.read_bytes()
            if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise FileCatalogError("Целостность файла не подтверждена в транзакции")
            db.execute("UPDATE gd_files SET state='manual_required',order_id=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE file_id=?", (order_id, file_id))
        return {"file_id": file_id, "order_id": order_id, "status": "MANUAL_REQUIRED", "reason": "Передача файла не автоматизируется без подтверждённого adapter"}
