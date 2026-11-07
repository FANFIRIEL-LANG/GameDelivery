from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.main import MainWindow
from app.proxy_settings import _database_path


class ProxySettingsDatabasePathTests(unittest.TestCase):
    def test_proxy_settings_share_the_orders_database_path(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            local_app_data = home / "AppData" / "Local"
            home.mkdir(parents=True)

            with patch("pathlib.Path.home", return_value=home), patch.dict(
                os.environ, {"LOCALAPPDATA": str(local_app_data)}
            ):
                proxy_database = _database_path()
                orders_database = MainWindow._database_path.fget(None)

            self.assertEqual(
                proxy_database,
                orders_database,
                "Proxy settings and orders must use one canonical local database",
            )


if __name__ == "__main__":
    unittest.main()
