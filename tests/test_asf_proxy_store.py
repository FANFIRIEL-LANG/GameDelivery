import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations.asf.proxy_config import SteamProxy
from integrations.asf.proxy_store import ProxyStore


class PasswordDeleteError(Exception):
    pass


class FakeKeyring:
    class errors:
        PasswordDeleteError = PasswordDeleteError
        PasswordNotFoundException = PasswordDeleteError

    def __init__(self, priority=1):
        self.priority = priority
        self.passwords = {}
        self.fail_set = False

    def get_keyring(self):
        return self

    def get_password(self, service, username):
        return self.passwords.get((service, username))

    def set_password(self, service, username, password):
        if self.fail_set:
            raise RuntimeError("simulated keyring failure")
        self.passwords[(service, username)] = password

    def delete_password(self, service, username):
        key = (service, username)
        if key not in self.passwords:
            raise PasswordDeleteError("missing")
        del self.passwords[key]


class ProxyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.database = Path(self.temp_dir.name) / "gamedelivery.sqlite3"
        self.store = ProxyStore(self.database)
        self.keyring = FakeKeyring()
        self.keyring_patch = patch.object(ProxyStore, "_keyring", return_value=self.keyring)
        self.keyring_patch.start()
        self.addCleanup(self.keyring_patch.stop)

    def test_profile_metadata_is_in_sqlite_but_password_is_only_in_keyring(self):
        proxy = SteamProxy("socks5", "proxy.example", 1080, "proxy-user", "proxy-secret")

        self.store.save("bot-a", proxy)

        profile = self.store.list_profiles()[0]
        self.assertEqual(profile["bot_name"], "bot-a")
        self.assertEqual(profile["host"], "proxy.example")
        self.assertEqual(profile["username"], "proxy-user")
        self.assertNotIn("password", profile)
        self.assertEqual(self.store.get_password("bot-a"), "proxy-secret")
        with sqlite3.connect(self.database) as db:
            raw = repr(db.execute("SELECT * FROM steam_proxy_profiles").fetchall())
        self.assertNotIn("proxy-secret", raw)

    def test_missing_secure_keyring_fails_closed_before_database_write(self):
        self.keyring.priority = 0

        with self.assertRaisesRegex(RuntimeError, "недоступно"):
            self.store.save("bot-a", SteamProxy("http", "proxy.example", 8080, "user", "secret"))

        self.assertEqual(self.store.list_profiles(), [])
        self.assertEqual(self.keyring.passwords, {})

    def test_failed_database_write_restores_previous_keyring_password(self):
        self.keyring.passwords[("GameDelivery.ASFProxy", "bot-a:password")] = "old-secret"
        original_connect = self.store._connect

        with patch.object(self.store, "_connect", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "disk full"):
                self.store.save(
                    "bot-a",
                    SteamProxy("socks5", "new.example", 1080, "user", "new-secret"),
                )

        self.assertEqual(
            self.keyring.get_password("GameDelivery.ASFProxy", "bot-a:password"),
            "old-secret",
        )
        self.assertEqual(self.store.list_profiles(), [])

    def test_saving_proxy_without_auth_removes_stale_password(self):
        self.store.save("bot-a", SteamProxy("http", "proxy.example", 8080, "user", "old-secret"))
        self.assertEqual(self.store.get_password("bot-a"), "old-secret")

        self.store.save("bot-a", SteamProxy("http", "proxy.example", 8080))

        self.assertIsNone(self.store.get_password("bot-a"))
        self.assertIsNone(self.keyring.get_password("GameDelivery.ASFProxy", "bot-a:password"))
        self.assertIsNone(self.store.list_profiles()[0]["username"])

    def test_proxy_pool_keeps_password_out_of_sqlite_and_deletes_keyring_entry(self):
        proxy = SteamProxy("socks5", "pool.example", 1081, "pool-user", "pool-secret")

        ids = self.store.add_pool_proxies([proxy])

        self.assertEqual(len(ids), 1)
        self.assertEqual(self.store.get_pool_password(ids[0]), "pool-secret")
        with sqlite3.connect(self.database) as db:
            raw = repr(db.execute("SELECT * FROM steam_proxy_pool").fetchall())
        self.assertNotIn("pool-secret", raw)

        self.store.delete_pool_proxy(ids[0])

        self.assertEqual(self.store.list_pool(), [])
        self.assertIsNone(
            self.keyring.get_password("GameDelivery.ASFProxy", f"pool:{ids[0]}:password")
        )

    def test_pool_import_fails_closed_if_keyring_write_fails(self):
        self.keyring.fail_set = True
        proxy = SteamProxy("http", "pool.example", 8080, "user", "secret")

        with self.assertRaisesRegex(RuntimeError, "сохранить список прокси"):
            self.store.add_pool_proxies([proxy])

        self.assertEqual(self.store.list_pool(), [])
        self.assertEqual(self.keyring.passwords, {})

    def test_get_password_requires_existing_profile(self):
        with self.assertRaisesRegex(KeyError, "не найден"):
            self.store.get_password("missing-bot")


if __name__ == "__main__":
    unittest.main()
