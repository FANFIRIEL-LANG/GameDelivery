from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SERVICE_NAME = "GameDelivery.ASFProxy"


class ProxyStore:
    """Store proxy metadata in SQLite and credentials in the OS keyring."""

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS steam_proxy_profiles (
                    bot_name TEXT PRIMARY KEY,
                    scheme TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL DEFAULT
                        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                )
                """
            )

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError as exc:
            raise RuntimeError("Для безопасного хранения паролей установи пакет keyring") from exc
        return keyring

    def save(self, bot_name: str, proxy: Any, *, enabled: bool = True) -> None:
        bot_name = bot_name.strip()
        if not bot_name:
            raise ValueError("Укажи имя бота ASF")
        keyring = self._keyring()
        # Fail closed if the OS credential backend is unavailable or unusable.
        backend = keyring.get_keyring()
        priority = getattr(backend, "priority", 0)
        if not priority or priority < 1:
            raise RuntimeError("Системное хранилище учётных данных недоступно; пароль не сохранён")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO steam_proxy_profiles
                    (bot_name, scheme, host, port, username, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                ON CONFLICT(bot_name) DO UPDATE SET
                    scheme=excluded.scheme, host=excluded.host, port=excluded.port,
                    username=excluded.username, enabled=excluded.enabled,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                """,
                (bot_name, proxy.scheme, proxy.host, proxy.port, proxy.username, int(enabled)),
            )
        password_key = f"{bot_name}:password"
        if proxy.password is not None:
            keyring.set_password(SERVICE_NAME, password_key, proxy.password)
        else:
            try:
                keyring.delete_password(SERVICE_NAME, password_key)
            except keyring.errors.PasswordDeleteError:
                pass

    def list_profiles(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT bot_name, scheme, host, port, username, enabled, updated_at
                FROM steam_proxy_profiles ORDER BY bot_name COLLATE NOCASE
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_password(self, bot_name: str) -> str | None:
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM steam_proxy_profiles WHERE bot_name = ?", (bot_name,)
            ).fetchone()
        if exists is None:
            raise KeyError("Профиль прокси не найден")
        return self._keyring().get_password(SERVICE_NAME, f"{bot_name}:password")

    def delete(self, bot_name: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM steam_proxy_profiles WHERE bot_name = ?", (bot_name,))
        keyring = self._keyring()
        try:
            keyring.delete_password(SERVICE_NAME, f"{bot_name}:password")
        except keyring.errors.PasswordDeleteError:
            pass
