from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit/rollback and always release the database handle on context exit."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect_sqlite(
    database: str | Path,
    *,
    timeout: float = 10.0,
    row_factory: Any = sqlite3.Row,
) -> sqlite3.Connection:
    """Open a connection with connection-local foreign-key enforcement enabled."""
    connection = sqlite3.connect(str(database), timeout=timeout, factory=ClosingSQLiteConnection)
    connection.row_factory = row_factory
    connection.execute("PRAGMA foreign_keys = ON")
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()
    if enabled is None or int(enabled[0]) != 1:
        connection.close()
        raise sqlite3.OperationalError("SQLite foreign-key enforcement could not be enabled")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection
