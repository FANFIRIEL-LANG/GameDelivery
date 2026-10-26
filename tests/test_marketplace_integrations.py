import unittest
from datetime import datetime, timezone

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
        if (service, account) not in self.values:
            raise KeyError("not found")
        del self.values[(service, account)]


class FakePlayerokTransport:
    def __init__(self, *, requires_two_factor=False):
        self.requires_two_factor = requires_two_factor
        self.calls = []
        self.logout_called = False

    def __call__(self, url, *, method="GET", payload=None, token=None, bearer=False):
        self.calls.append({
            "url": url, "method": method, "payload": payload,
            "token": token, "bearer": bearer,
        })
        if url.endswith("/auth/send-otp"):
            return {}, None
        if url.endswith("/auth/confirm-otp"):
            if self.requires_two_factor:
                return {
                    "requiresTwoFactor": True,
                    "secondFactorSession": {"token": "challenge-session"},
                }, None
            return {"token": "session-secret"}, None
        if url.endswith("/auth/confirm-second-factor"):
            if payload != {"token": "challenge-session", "totpCode": "654321"}:
                raise ValueError("wrong second factor")
            return {"token": "session-2fa-secret"}, None
        if url.endswith("/viewer"):
            if token not in {"session-secret", "session-2fa-secret", "saved-secret"}:
                raise ValueError("unauthorized")
            return {"username": "seller-demo", "id": "user-10"}, None
        if url.endswith("/graphql"):
            return {
                "data": {
                    "deals": {
                        "edges": [{
                            "node": {
                                "id": "deal-123",
                                "status": "PAID",
                                "item": {"id": "item-1", "name": "Steam Card Set"},
                                "user": {"id": "buyer-1", "username": "buyer-a"},
                                "transaction": {"value": 250},
                                "createdAt": datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat(),
                                "url": "https://playerok.com/deal/deal-123",
                            }
                        }],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "totalCount": 1,
                    }
                }
            }, None
        if url.endswith("/auth/logout"):
            self.logout_called = True
            return {}, None
        raise AssertionError(f"unexpected endpoint: {url}")


class MarketplaceRegistryTests(unittest.TestCase):
    def test_registry_uses_normalized_id_and_rejects_duplicates(self):
        adapter = PlayerokAdapter(
            request_func=FakePlayerokTransport(),
            keyring_backend=FakeKeyring(),
        )
        registry = MarketplaceRegistry()
        registry.register(adapter)
        self.assertIs(registry.get(" PLAYEROK "), adapter)
        self.assertEqual(registry.list_platforms()[0].display_name, "Playerok")
        with self.assertRaises(ValueError):
            registry.register(adapter)


class PlayerokAdapterTests(unittest.TestCase):
    def setUp(self):
        self.keyring = FakeKeyring()
        self.transport = FakePlayerokTransport()
        self.adapter = PlayerokAdapter(
            request_func=self.transport,
            keyring_backend=self.keyring,
        )

    def test_email_login_persists_token_and_uses_own_https_requests(self):
        self.adapter.request_login_code("seller@example.com")
        send_call = self.transport.calls[0]
        self.assertEqual(send_call["url"], "https://playerok.com/rest-api/public/auth/send-otp")
        self.assertEqual(send_call["payload"], {"email": "seller@example.com"})

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
        self.assertEqual(orders[0]["url"], "https://playerok.com/deal/deal-123")
        self.assertNotIn("session-secret", str(orders))
        gql_call = next(call for call in self.transport.calls if call["url"].endswith("/graphql"))
        self.assertEqual(gql_call["payload"]["operationName"], "Deals")
        self.assertEqual(gql_call["payload"]["variables"]["filter"]["direction"], "OUT")
        self.assertEqual(gql_call["payload"]["variables"]["filter"]["status"], ["PAID", "SENT"])

    def test_two_factor_flow_does_not_persist_until_completed(self):
        self.transport.requires_two_factor = True
        pending = self.adapter.confirm_login_code("seller@example.com", "123456")
        self.assertTrue(pending["requires_second_factor"])
        self.assertFalse(self.adapter.has_saved_session())

        profile = self.adapter.confirm_second_factor("654321")
        self.assertEqual(profile["username"], "seller-demo")
        self.assertTrue(profile["session_saved"])
        self.assertTrue(self.adapter.has_saved_session())

    def test_disconnect_clears_local_session_and_requests_remote_logout(self):
        self.keyring.set_password("GameDelivery", "playerok.session", "saved-secret")
        result = self.adapter.disconnect()
        self.assertFalse(result["connected"])
        self.assertTrue(result["local_session_cleared"])
        self.assertFalse(self.adapter.has_saved_session())
        self.assertTrue(self.transport.logout_called)

    def test_invalid_email_is_rejected_before_network_call(self):
        with self.assertRaises(ValueError):
            self.adapter.request_login_code("not-an-email")
        self.assertEqual(self.transport.calls, [])

    def test_non_https_foreign_hosts_and_malformed_tokens_are_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter._request("http://playerok.com/rest-api/public/auth/send-otp")
        with self.assertRaises(ValueError):
            self.adapter._request("https://attacker.example/auth/send-otp")
        with self.assertRaises(RuntimeError):
            self.adapter._request(
                "https://playerok.com/rest-api/public/viewer", token="token\\r\\nInjected: true"
            )


if __name__ == "__main__":
    unittest.main()
