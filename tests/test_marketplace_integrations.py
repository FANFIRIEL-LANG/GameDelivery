import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from integrations.marketplaces import MarketplaceRegistry
from integrations.playerok.client import PlayerokAdapter


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, password):
        self.values[(service, account)] = password

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class FakeAuth:
    def __init__(self, account):
        self.account = account

    async def send_otp(self, email):
        FakeAccount.last_email = email

    async def confirm_otp(self, email, code):
        if FakeAccount.requires_two_factor:
            return SimpleNamespace(
                requires_two_factor=True,
                second_factor_token="challenge-session",
                token=None,
            )
        self.account.token = "session-secret"
        return SimpleNamespace(
            requires_two_factor=False,
            second_factor_token=None,
            token="session-secret",
        )

    async def confirm_second_factor(self, session, code):
        if session != "challenge-session" or code != "654321":
            raise ValueError("wrong second factor")
        self.account.token = "session-2fa-secret"
        return self.account.token

    async def logout(self):
        FakeAccount.logout_called = True
        self.account.token = None


class FakeDeals:
    async def search(self, *, filter, first):
        if filter != {"direction": "OUT", "status": ["PAID", "SENT"]}:
            raise AssertionError("unexpected deal filter")
        self.first = first
        return [
            SimpleNamespace(
                id="deal-123",
                status=SimpleNamespace(value="PAID"),
                item=SimpleNamespace(name="Steam Card Set"),
                user=SimpleNamespace(username="buyer-a"),
                transaction=SimpleNamespace(value=250),
                created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                url="https://playerok.com/deal/deal-123",
            )
        ]


class FakeAccount:
    requires_two_factor = False
    last_email = None
    logout_called = False

    def __init__(self, token=None):
        self.token = token
        self.auth = FakeAuth(self)
        self.deals = FakeDeals()

    async def get_me(self):
        if not self.token:
            raise ValueError("no session")
        return SimpleNamespace(username="seller-demo", id="user-10")

    async def aclose(self):
        return None


class MarketplaceRegistryTests(unittest.TestCase):
    def test_registry_uses_normalized_id_and_rejects_duplicates(self):
        adapter = PlayerokAdapter(account_factory=FakeAccount, keyring_backend=FakeKeyring())
        registry = MarketplaceRegistry()
        registry.register(adapter)
        self.assertIs(registry.get(" PLAYEROK "), adapter)
        self.assertEqual(registry.list_platforms()[0].display_name, "Playerok")
        with self.assertRaises(ValueError):
            registry.register(adapter)


class PlayerokAdapterTests(unittest.TestCase):
    def setUp(self):
        FakeAccount.requires_two_factor = False
        FakeAccount.last_email = None
        FakeAccount.logout_called = False
        self.keyring = FakeKeyring()
        self.adapter = PlayerokAdapter(
            account_factory=FakeAccount,
            keyring_backend=self.keyring,
        )

    def test_email_login_persists_token_to_keyring_and_fetches_sales(self):
        self.adapter.request_login_code("seller@example.com")
        self.assertEqual(FakeAccount.last_email, "seller@example.com")

        profile = self.adapter.confirm_login_code("seller@example.com", "123456")
        self.assertTrue(profile["connected"])
        self.assertEqual(profile["username"], "seller-demo")
        self.assertTrue(profile["session_saved"])
        self.assertNotIn("session-secret", str(profile))
        self.assertTrue(self.adapter.has_saved_session())

        restored = self.adapter.restore_session()
        self.assertEqual(restored["username"], "seller-demo")
        orders = self.adapter.fetch_orders()
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["order_id"], "deal-123")
        self.assertEqual(orders[0]["title"], "Steam Card Set")
        self.assertEqual(orders[0]["buyer"], "buyer-a")
        self.assertEqual(orders[0]["status"], "PAID")
        self.assertNotIn("session-secret", str(orders))

    def test_two_factor_flow_does_not_persist_until_completed(self):
        FakeAccount.requires_two_factor = True
        pending = self.adapter.confirm_login_code("seller@example.com", "123456")
        self.assertTrue(pending["requires_second_factor"])
        self.assertFalse(self.adapter.has_saved_session())

        profile = self.adapter.confirm_second_factor("654321")
        self.assertEqual(profile["username"], "seller-demo")
        self.assertTrue(profile["session_saved"])
        self.assertTrue(self.adapter.has_saved_session())

    def test_disconnect_removes_local_session_and_requests_remote_logout(self):
        self.keyring.set_password("GameDelivery", "playerok.session", "session-secret")
        result = self.adapter.disconnect()
        self.assertFalse(result["connected"])
        self.assertTrue(result["local_session_cleared"])
        self.assertFalse(self.adapter.has_saved_session())
        self.assertTrue(FakeAccount.logout_called)

    def test_invalid_email_is_rejected_before_network_call(self):
        with self.assertRaises(ValueError):
            self.adapter.request_login_code("not-an-email")
        self.assertIsNone(FakeAccount.last_email)


if __name__ == "__main__":
    unittest.main()
