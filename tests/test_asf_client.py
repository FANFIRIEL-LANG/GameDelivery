import json
import unittest
from unittest.mock import patch

from integrations.asf.client import ASFClient, ASFError


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class ASFClientTests(unittest.TestCase):
    @patch("integrations.asf.client.urlopen")
    def test_status_uses_local_ipc_and_auth_header(self, urlopen):
        urlopen.return_value = FakeResponse({"Success": True})
        client = ASFClient(ipc_password="local-secret")
        self.assertEqual(client.get_status(), {"Success": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:1242/Api/ASF")
        self.assertEqual(request.get_header("Authentication"), "local-secret")

    def test_rejects_non_api_path(self):
        client = ASFClient()
        with self.assertRaises(ValueError):
            client.request("http://example.com/")

    def test_empty_bot_name_rejected(self):
        with self.assertRaises(ValueError):
            ASFClient().get_bot(" ")

    @patch("integrations.asf.client.urlopen")
    def test_unauthorized_response_does_not_expose_secret(self, urlopen):
        from urllib.error import HTTPError
        urlopen.side_effect = HTTPError(
            "http://127.0.0.1:1242/Api/ASF", 401, "Unauthorized", {}, None
        )
        with self.assertRaises(ASFError) as raised:
            ASFClient(ipc_password="do-not-print").get_status()
        self.assertNotIn("do-not-print", str(raised.exception))
        self.assertIn("авторизацию", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
