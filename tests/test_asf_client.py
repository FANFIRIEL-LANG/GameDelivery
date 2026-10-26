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
        with self.assertRaises(ValueError):
            ASFClient().request("http://example.com/")

    def test_empty_bot_name_rejected(self):
        with self.assertRaises(ValueError):
            ASFClient().get_bot(" ")

    def test_rejects_non_local_ipc(self):
        with self.assertRaises(ValueError):
            ASFClient(base_url="http://192.168.1.10:1242").get_status()

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

    @patch("integrations.asf.client.urlopen")
    def test_apply_proxy_preserves_existing_bot_settings(self, urlopen):
        existing = {
            "AcceptGifts": True,
            "Enabled": True,
            "SteamLogin": "account-name",
            "SteamTradeToken": "tradetoken",
            "WebProxy": None,
            "WebProxyUsername": None,
            "WebProxyPassword": None,
            "SteamProtocols": 7,
        }
        urlopen.side_effect = [
            FakeResponse({
                "Success": True,
                "Result": {
                    "bot-a": {"BotConfig": existing, "BotName": "bot-a"}
                },
            }),
            FakeResponse({"Success": True, "Result": {"bot-a": True}}),
        ]
        client = ASFClient()
        summary = client.apply_proxy_settings("bot-a", {
            "WebProxy": "socks5://proxy.example:1080",
            "WebProxyUsername": "user",
            "WebProxyPassword": "secret",
        })
        self.assertTrue(summary["saved"])
        posted = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))
        config = posted["BotConfig"]
        self.assertTrue(config["AcceptGifts"])
        self.assertTrue(config["Enabled"])
        self.assertEqual(config["SteamLogin"], "account-name")
        self.assertEqual(config["SteamTradeToken"], "tradetoken")
        self.assertEqual(config["WebProxy"], "socks5://proxy.example:1080")
        self.assertEqual(config["SteamProtocols"], 7)
        self.assertTrue(summary["steam_connection_proxy_requires_global_setting"])
        self.assertNotIn("secret", str(summary))

    @patch("integrations.asf.client.urlopen")
    def test_refuses_to_write_when_bot_config_missing(self, urlopen):
        urlopen.return_value = FakeResponse({"Success": True, "Result": {"bot-a": {"BotName": "bot-a"}}})
        with self.assertRaises(ASFError):
            ASFClient().apply_proxy_settings("bot-a", {
                "WebProxy": "http://proxy.example:8080",
                "WebProxyUsername": None,
                "WebProxyPassword": None,
            })
        self.assertEqual(urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
