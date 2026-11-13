from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def connect_sqlite(
    database: str | Path,
    *,
    timeout: float = 10.0,
    row_factory: Any = sqlite3.Row,
) -> sqlite3.Connection:
    """Open a connection with connection-local foreign-key enforcement enabled."""
    connection = sqlite3.connect(str(database), timeout=timeout)
    connection.row_factory = row_factory
    connection.execute("PRAGMA foreign_keys = ON")
    enabled = connection.execute("PRAGMA foreign_keys").fetchone()
    if enabled is None or int(enabled[0]) != 1:
        connection.close()
        raise sqlite3.OperationalError("SQLite foreign-key enforcement could not be enabled")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection
