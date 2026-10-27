from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox
from app.main import MainWindow


class DatabaseStartupFailureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_corrupt_saved_database_is_not_reset_and_ui_does_not_show_raw_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "broken.sqlite3"
            original = b"synthetic-corrupt-database-sentinel"
            database.write_bytes(original)
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.critical") as critical:
                with self.assertRaises(sqlite3.DatabaseError):
                    MainWindow()
                self.assertTrue(critical.called)
                displayed = "\n".join(str(arg) for arg in critical.call_args.args)
                self.assertNotIn("synthetic-corrupt-database-sentinel", displayed)
            self.assertEqual(database.read_bytes(), original)

    def test_database_permission_failure_is_reported_without_raw_os_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "state.sqlite3"
            marker = "synthetic-filesystem-error-sentinel"
            with patch.object(MainWindow, "_database_path", new_callable=PropertyMock, return_value=database), \
                 patch("app.main.QMessageBox.critical") as critical, \
                 patch("sqlite3.connect", side_effect=OSError(marker)):
                with self.assertRaises(OSError):
                    MainWindow()
                self.assertTrue(critical.called)
                displayed = "\n".join(str(arg) for arg in critical.call_args.args)
                self.assertNotIn(marker, displayed)


if __name__ == "__main__":
    unittest.main()
