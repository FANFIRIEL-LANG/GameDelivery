import sys
import types
import unittest
from unittest.mock import patch

from integrations.asf import trade_credentials
from integrations.asf.trade_credentials import SteamTradeCredentialStore


class MemoryKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class BrokenKeyring(MemoryKeyring):
    def set_password(self, service, account, password):
        raise RuntimeError("backend unavailable")


class SteamTradeCredentialStoreTests(unittest.TestCase):
    ORDER_ID = "GD-TRADE-SECRET-1"
    TRADE_URL = "https://steamcommunity.com/tradeoffer/new/?partner=12345&token=Tok-abc_1"
    SAFE_URL = "https://steamcommunity.com/tradeoffer/new/?partner=12345"

    def test_windows_uses_explicit_credential_manager_backend(self):
        windows_module = types.ModuleType("keyring.backends.Windows")

        class FakeWinVaultKeyring:
            pass

        windows_module.WinVaultKeyring = FakeWinVaultKeyring
        keyring_module = types.ModuleType("keyring")
        keyring_module.__path__ = []
        backends_module = types.ModuleType("keyring.backends")
        backends_module.__path__ = []

        with patch.object(trade_credentials.os, "name", "nt"):
            with patch.dict(
                sys.modules,
                {
                    "keyring": keyring_module,
                    "keyring.backends": backends_module,
                    "keyring.backends.Windows": windows_module,
                },
            ):
                store = SteamTradeCredentialStore()

        self.assertIsInstance(store._backend, FakeWinVaultKeyring)

    def test_windows_fails_closed_when_credential_manager_backend_is_missing(self):
        keyring_module = types.ModuleType("keyring")
        keyring_module.__path__ = []
        backends_module = types.ModuleType("keyring.backends")
        backends_module.__path__ = []

        with patch.object(trade_credentials.os, "name", "nt"):
            with patch.dict(
                sys.modules,
                {
                    "keyring": keyring_module,
                    "keyring.backends": backends_module,
                    "keyring.backends.Windows": None,
                },
            ):
                with self.assertRaisesRegex(OSError, "Credential Manager"):
                    SteamTradeCredentialStore()

    def test_store_keeps_only_partner_in_database_value_and_restores_url(self):
        backend = MemoryKeyring()
        store = SteamTradeCredentialStore(backend=backend)

        safe_url = store.store_trade_url(self.ORDER_ID, self.TRADE_URL)

        self.assertEqual(safe_url, self.SAFE_URL)
        self.assertNotIn("Tok-abc_1", safe_url)
        self.assertEqual(store.load_trade_url(self.ORDER_ID, safe_url), self.TRADE_URL)
        self.assertEqual(len(backend.values), 1)
        self.assertNotIn("Tok-abc_1", repr(safe_url))

    def test_migrate_legacy_url_moves_token_to_keyring_before_returning_safe_url(self):
        backend = MemoryKeyring()
        store = SteamTradeCredentialStore(backend=backend)

        safe_url = store.migrate_legacy_url(self.ORDER_ID, self.TRADE_URL)

        self.assertEqual(safe_url, self.SAFE_URL)
        self.assertEqual(store.load_trade_url(self.ORDER_ID, safe_url), self.TRADE_URL)

    def test_load_fails_closed_when_secure_token_is_missing(self):
        store = SteamTradeCredentialStore(backend=MemoryKeyring())

        with self.assertRaisesRegex(ValueError, "системном хранилище"):
            store.load_trade_url(self.ORDER_ID, self.SAFE_URL)

    def test_load_rejects_inline_legacy_token_until_migration_runs(self):
        backend = MemoryKeyring()
        store = SteamTradeCredentialStore(backend=backend)

        with self.assertRaisesRegex(ValueError, "не перенесена"):
            store.load_trade_url(self.ORDER_ID, self.TRADE_URL)

        self.assertEqual(backend.values, {})

    def test_store_fails_closed_when_keyring_is_unavailable(self):
        store = SteamTradeCredentialStore(backend=BrokenKeyring())

        with self.assertRaisesRegex(OSError, "системном хранилище"):
            store.store_trade_url(self.ORDER_ID, self.TRADE_URL)

    def test_delete_removes_order_token(self):
        backend = MemoryKeyring()
        store = SteamTradeCredentialStore(backend=backend)
        store.store_trade_url(self.ORDER_ID, self.TRADE_URL)

        store.delete_trade_url(self.ORDER_ID)

        self.assertIsNone(backend.get_password(store.SERVICE, self.ORDER_ID))


if __name__ == "__main__":
    unittest.main()
