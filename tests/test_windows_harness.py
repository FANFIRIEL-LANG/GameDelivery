from __future__ import annotations

import ast
import builtins
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app.harness import HarnessIsolationError, HarnessRuntime


ROOT = Path(__file__).resolve().parents[1]
HARNESS_SOURCE = ROOT / "app" / "harness.py"
ENTRY_SOURCE = ROOT / "app" / "__main__.py"


def _import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class HarnessBootstrapTests(unittest.TestCase):
    def test_harness_branch_runs_before_app_main_import(self):
        from app import __main__ as entry

        fake_harness = types.ModuleType("app.harness")
        fake_harness.run_harness = lambda: 17
        original_import = builtins.__import__
        imported: list[str] = []

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            imported.append(name)
            if name == "app.main":
                raise AssertionError("MainWindow production graph imported in harness mode")
            return original_import(name, globals, locals, fromlist, level)

        with patch.dict(sys.modules, {"app.harness": fake_harness}), patch(
            "builtins.__import__", side_effect=guarded_import
        ):
            self.assertEqual(entry._dispatch(["--harness"]), 17)
        self.assertIn("app.harness", imported)
        self.assertNotIn("app.main", imported)

    def test_ordinary_dispatch_still_calls_production_main(self):
        from app import __main__ as entry

        calls: list[str] = []
        fake_main = types.ModuleType("app.main")
        fake_main.MainWindow = object
        fake_main.main = lambda: calls.append("main")
        with patch.dict(sys.modules, {"app.main": fake_main}):
            self.assertEqual(entry._dispatch([]), 0)
        self.assertEqual(calls, ["main"])


class HarnessIsolationTests(unittest.TestCase):
    def test_all_runtime_paths_are_inside_explicit_root(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            runtime = HarnessRuntime.from_root(Path(temporary_root) / "isolated")
            paths = [runtime.root, runtime.database_path]
            for path in paths:
                self.assertTrue(path.is_relative_to(runtime.root))
            self.assertEqual(runtime.database_path, runtime.root / "data" / "harness.sqlite3")

    def test_path_outside_root_is_rejected_without_fallback(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            runtime = HarnessRuntime.from_root(Path(temporary_root) / "isolated")
            with self.assertRaises(HarnessIsolationError):
                runtime.path("..", "user", ".gamedelivery", "gamedelivery.sqlite3")

    def test_synthetic_database_is_created_only_under_root(self):
        with tempfile.TemporaryDirectory() as temporary_root:
            runtime = HarnessRuntime.from_root(Path(temporary_root) / "isolated")
            runtime.initialize_synthetic_database()
            self.assertTrue(runtime.database_path.is_file())
            self.assertTrue(runtime.database_path.is_relative_to(runtime.root))
            connection = sqlite3.connect(runtime.database_path)
            try:
                columns = connection.execute("PRAGMA table_info(harness_probe)").fetchall()
            finally:
                connection.close()
            self.assertEqual([column[1] for column in columns], ["probe_key", "probe_value"])

    def test_harness_has_no_qsettings_dependency(self):
        imports = _import_names(HARNESS_SOURCE)
        self.assertFalse(any("QSettings" in name or name.endswith("settings") for name in imports))
        source = HARNESS_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("QSettings(", source)

    def test_harness_does_not_import_integration_or_network_components(self):
        imports = _import_names(HARNESS_SOURCE)
        forbidden = (
            "app.main", "app.asf_management", "app.playerok_browser",
            "integrations", "requests", "urllib", "http", "PySide6.QtNetwork",
            "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        )
        for module in imports:
            self.assertFalse(any(module == prefix or module.startswith(prefix + ".")
                                 for prefix in forbidden), module)

    def test_harness_creates_no_recovery_reconciliation_or_background_jobs(self):
        source = HARNESS_SOURCE.read_text(encoding="utf-8")
        imports = _import_names(HARNESS_SOURCE)
        self.assertNotIn("QTimer", source)
        self.assertNotIn("QThreadPool", source)
        self.assertNotIn("DeliveryReconciliation", source)
        self.assertNotIn("recover_interrupted", source)
        self.assertNotIn("QTimer", imports)

    def test_harness_entry_has_no_home_or_user_settings_fallback(self):
        source = HARNESS_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("Path.home", source)
        self.assertNotIn("USERPROFILE", source)
        self.assertNotIn("APPDATA", source)

    def test_harness_entry_is_lazy_and_normal_path_is_preserved(self):
        source = ENTRY_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("from app.main import", source.split("def _dispatch", 1)[0])
        self.assertLess(source.index('if "--harness" in args:'), source.index("from app.main import MainWindow, main"))
        self.assertIn('if "--self-test" in args:', source)
        self.assertIn("    main()", source)


if __name__ == "__main__":
    unittest.main()
