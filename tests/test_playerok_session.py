import unittest

from integrations.playerok.client import PlayerokAdapter, _BFF


class MemoryKeyring:
    def __init__(self, token=None, fail_delete=False, fail_get=False):
        self.token = token
        self.fail_delete = fail_delete
        self.fail_get = fail_get

    def get_password(self, service, account):
        if self.fail_get:
            raise RuntimeError("keyring read failed")
        return self.token

    def set_password(self, service, account, value):
        self.token = value

    def delete_password(self, service, account):
        if self.fail_delete:
            raise RuntimeError("keyring delete failed")
        if self.token is None:
            raise RuntimeError("no stored credential")
        self.token = None


class PlayerokSessionTests(unittest.TestCase):
    def test_restore_session_loads_saved_token_and_validates_profile(self):
        keyring = MemoryKeyring("saved-session-token")
        requests = []

        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            requests.append((url, method, payload, token, bearer))
            return ({
                "success": True,
                "data": {"id": "seller-17", "username": "seller_name"},
            }, None)

        adapter = PlayerokAdapter(
            keyring_backend=keyring,
            request_func=fake_request,
        )
        restored = adapter.restore_session()

        self.assertEqual(restored["user_id"], "seller-17")
        self.assertEqual(restored["username"], "seller_name")
        self.assertTrue(restored["connected"])
        self.assertTrue(restored["session_saved"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], _BFF + "/viewer")
        self.assertEqual(requests[0][3], "saved-session-token")
        self.assertTrue(requests[0][4])

    def test_disconnect_clears_local_token_even_if_remote_logout_fails(self):
        keyring = MemoryKeyring("saved-session-token")
        requests = []

        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            requests.append((url, method, payload, token, bearer))
            raise RuntimeError("Playerok вернул HTTP 503.")

        adapter = PlayerokAdapter(
            keyring_backend=keyring,
            request_func=fake_request,
        )
        result = adapter.disconnect()

        self.assertFalse(result["connected"])
        self.assertTrue(result["local_session_cleared"])
        self.assertIsNone(keyring.token)
        self.assertIsNone(adapter._session_token)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], _BFF + "/auth/logout")
        self.assertEqual(requests[0][1], "POST")
        self.assertEqual(requests[0][3], "saved-session-token")
        self.assertTrue(requests[0][4])
        self.assertIn("не подтвердил удалённый выход", result["message"])
        self.assertNotIn("saved-session-token", result["message"])

    def test_restore_session_reports_keyring_read_failure(self):
        keyring = MemoryKeyring("saved-session-token", fail_get=True)

        adapter = PlayerokAdapter(
            keyring_backend=keyring,
            request_func=lambda *args, **kwargs: ({}, None),
        )
        with self.assertRaisesRegex(RuntimeError, "Не удалось прочитать сессию"):
            adapter.restore_session()

    def test_disconnect_does_not_claim_clear_if_keyring_read_and_delete_fail(self):
        keyring = MemoryKeyring(
            "saved-session-token",
            fail_delete=True,
            fail_get=True,
        )
        requests = []

        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            requests.append(url)
            return ({}, None)

        adapter = PlayerokAdapter(
            keyring_backend=keyring,
            request_func=fake_request,
        )
        result = adapter.disconnect()

        self.assertFalse(result["local_session_cleared"])
        self.assertEqual(keyring.token, "saved-session-token")
        self.assertEqual(requests, [])
        self.assertIn("Не удалось удалить токен", result["message"])

    def test_disconnect_reports_when_keyring_cannot_delete_saved_token(self):
        keyring = MemoryKeyring("saved-session-token", fail_delete=True)

        def fake_request(url, *, method="GET", payload=None, token=None, bearer=False):
            return ({}, None)

        adapter = PlayerokAdapter(
            keyring_backend=keyring,
            request_func=fake_request,
        )
        result = adapter.disconnect()

        self.assertFalse(result["local_session_cleared"])
        self.assertEqual(keyring.token, "saved-session-token")
        self.assertIn("Не удалось удалить токен", result["message"])


if __name__ == "__main__":
    unittest.main()
