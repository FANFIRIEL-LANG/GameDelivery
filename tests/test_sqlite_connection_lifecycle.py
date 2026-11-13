from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.database.connection import connect_sqlite


class SQLiteConnectionLifecycleTests(unittest.TestCase):
    def test_context_exit_closes_connection_after_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "close.sqlite3"
            with connect_sqlite(path) as connection:
                connection.execute("CREATE TABLE sample(value INTEGER)")
                connection.execute("INSERT INTO sample VALUES(1)")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            with connect_sqlite(path) as reopened:
                self.assertEqual(reopened.execute("SELECT value FROM sample").fetchone()[0], 1)

    def test_context_exit_closes_connection_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_sqlite(Path(directory) / "rollback.sqlite3")
            with self.assertRaisesRegex(RuntimeError, "expected"):
                with connection:
                    connection.execute("CREATE TABLE sample(value INTEGER)")
                    raise RuntimeError("expected")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")


if __name__ == "__main__":
    unittest.main()
