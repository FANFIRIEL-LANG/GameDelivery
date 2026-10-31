from __future__ import annotations

import sqlite3
import uuid
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS steam_proxy_pool (
                    proxy_id TEXT PRIMARY KEY,
                    scheme TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    username TEXT,
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

    @staticmethod
    def _delete_secret(keyring: Any, username: str) -> None:
        """Delete a keyring entry if present, without hiding backend failures."""
        try:
            keyring.delete_password(SERVICE_NAME, username)
        except Exception as exc:
            errors = getattr(keyring, "errors", None)
            missing_types = tuple(
                error_type
                for error_type in (
                    getattr(errors, "PasswordDeleteError", None),
                    getattr(errors, "PasswordNotFoundException", None),
                )
                if isinstance(error_type, type)
            )
            if missing_types and isinstance(exc, missing_types):
                return
            raise RuntimeError("Не удалось удалить пароль прокси из системного хранилища") from None

    def save(self, bot_name: str, proxy: Any, *, enabled: bool = True) -> None:
        bot_name = bot_name.strip()
        if not bot_name:
            raise ValueError("Укажи имя бота ASF")

        keyring = self._keyring()
        backend = keyring.get_keyring()
        priority = getattr(backend, "priority", 0)
        if not priority or priority < 1:
            raise RuntimeError("Системное хранилище учётных данных недоступно; пароль не сохранён")

        password_key = f"{bot_name}:password"
        try:
            previous_password = keyring.get_password(SERVICE_NAME, password_key)
        except Exception:
            raise RuntimeError("Не удалось проверить системное хранилище учётных данных") from None

        # Keep keyring and SQLite consistent across the update. When removing
        # authentication, delete the old secret before changing metadata; if SQLite
        # fails, restore the previous secret below. This fails closed on keyring errors.
        if proxy.password is not None:
            try:
                keyring.set_password(SERVICE_NAME, password_key, proxy.password)
            except Exception:
                raise RuntimeError("Не удалось сохранить пароль прокси в системном хранилище") from None
        else:
            self._delete_secret(keyring, password_key)

        try:
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
        except Exception as database_error:
            try:
                if previous_password is None:
                    self._delete_secret(keyring, password_key)
                else:
                    keyring.set_password(SERVICE_NAME, password_key, previous_password)
            except Exception:
                raise RuntimeError(
                    "Не удалось сохранить назначение прокси и восстановить прежний пароль; "
                    "проверь системное хранилище учётных данных"
                ) from database_error
            raise

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
        try:
            return self._keyring().get_password(SERVICE_NAME, f"{bot_name}:password")
        except Exception:
            raise RuntimeError("Не удалось прочитать пароль прокси из системного хранилища") from None

    def add_pool_proxies(self, proxies: list[Any]) -> list[str]:
        """Save a parsed proxy pool; credentials are kept only in the OS keyring."""
        if not proxies:
            raise ValueError("Список прокси пуст")
        keyring = self._keyring()
        backend = keyring.get_keyring()
        priority = getattr(backend, "priority", 0)
        if not priority or priority < 1:
            raise RuntimeError("Системное хранилище учётных данных недоступно; список не сохранён")
        records = [(uuid.uuid4().hex, proxy) for proxy in proxies]
        secrets_saved: list[str] = []
        try:
            for proxy_id, proxy in records:
                if proxy.password is not None:
                    keyring.set_password(SERVICE_NAME, f"pool:{proxy_id}:password", proxy.password)
                    secrets_saved.append(proxy_id)
            with self._connect() as connection:
                connection.executemany(
                    """
                    INSERT INTO steam_proxy_pool
                        (proxy_id, scheme, host, port, username, updated_at)
                    VALUES (?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
                    """,
                    [(proxy_id, proxy.scheme, proxy.host, proxy.port, proxy.username)
                     for proxy_id, proxy in records],
                )
        except Exception:
            for proxy_id in secrets_saved:
                try:
                    self._delete_secret(keyring, f"pool:{proxy_id}:password")
                except Exception:
                    pass
            raise RuntimeError("Не удалось сохранить список прокси; проверь системное хранилище") from None
        return [proxy_id for proxy_id, _ in records]

    def list_pool(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT proxy_id, scheme, host, port, username, updated_at
                FROM steam_proxy_pool ORDER BY rowid
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_pool_password(self, proxy_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username FROM steam_proxy_pool WHERE proxy_id = ?", (proxy_id,)
            ).fetchone()
        if row is None:
            raise KeyError("Прокси не найден")
        if not row["username"]:
            return None
        try:
            password = self._keyring().get_password(SERVICE_NAME, f"pool:{proxy_id}:password")
        except Exception:
            raise RuntimeError("Не удалось прочитать пароль прокси из системного хранилища") from None
        if not password:
            raise RuntimeError("Пароль выбранного прокси отсутствует в системном хранилище")
        return password

    def delete_pool_proxy(self, proxy_id: str) -> None:
        secret_name = f"pool:{proxy_id}:password"
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM steam_proxy_pool WHERE proxy_id = ?", (proxy_id,)
            ).fetchone()
        if exists is None:
            raise KeyError("Прокси не найден")

        keyring = self._keyring()
        try:
            previous_password = keyring.get_password(SERVICE_NAME, secret_name)
        except Exception:
            raise RuntimeError("Не удалось проверить пароль прокси в системном хранилище") from None
        self._delete_secret(keyring, secret_name)
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM steam_proxy_pool WHERE proxy_id = ?", (proxy_id,)
                )
                if cursor.rowcount == 0:
                    raise KeyError("Прокси не найден")
        except Exception as database_error:
            try:
                if previous_password is not None:
                    keyring.set_password(SERVICE_NAME, secret_name, previous_password)
            except Exception:
                raise RuntimeError(
                    "Не удалось удалить прокси из списка и восстановить его пароль; "
                    "проверь системное хранилище учётных данных"
                ) from database_error
            raise

    def delete(self, bot_name: str) -> None:
        secret_name = f"{bot_name}:password"
        keyring = self._keyring()
        try:
            previous_password = keyring.get_password(SERVICE_NAME, secret_name)
        except Exception:
            raise RuntimeError("Не удалось проверить пароль прокси в системном хранилище") from None
        self._delete_secret(keyring, secret_name)
        try:
            with self._connect() as connection:
                connection.execute("DELETE FROM steam_proxy_profiles WHERE bot_name = ?", (bot_name,))
        except Exception as database_error:
            try:
                if previous_password is not None:
                    keyring.set_password(SERVICE_NAME, secret_name, previous_password)
            except Exception:
                raise RuntimeError(
                    "Не удалось удалить назначение прокси и восстановить его пароль; "
                    "проверь системное хранилище учётных данных"
                ) from database_error
            raise
