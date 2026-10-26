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

    def test_rejects_localhost_prefix_spoofing_and_nonstandard_ports(self):
        for base_url in (
            "http://localhost.evil:1242",
            "http://127.0.0.1.evil:1242",
            "http://localhost:8080",
            "https://localhost:1242",
            "http://user@localhost:1242",
        ):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    ASFClient(base_url=base_url).get_status()

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
        }
        urlopen.side_effect = [
            FakeResponse({
                "Success": True,
                "Result": {
                    "bot-a": {"BotConfig": existing, "BotName": "bot-a"}
                },
            }),
            FakeResponse({"Success": True, "Result": {"bot-a": True}}),
            FakeResponse({
                "Success": True,
                "Result": {
                    "bot-a": {
                        "BotConfig": {
                            "AcceptGifts": True,
                            "Enabled": True,
                            "SteamLogin": "account-name",
                            "SteamTradeToken": "tradetoken",
                            "WebProxy": "socks5://proxy.example:1080",
                            "WebProxyUsername": "user",
                        },
                        "BotName": "bot-a",
                    }
                },
            }),
        ]
        client = ASFClient()
        summary = client.apply_proxy_settings("bot-a", {
            "WebProxy": "socks5://proxy.example:1080",
            "WebProxyUsername": "user",
            "WebProxyPassword": "secret",
        })
        self.assertTrue(summary["saved"])
        self.assertTrue(summary["verified"])
        posted = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))
        config = posted["BotConfig"]
        self.assertTrue(config["AcceptGifts"])
        self.assertTrue(config["Enabled"])
        self.assertEqual(config["SteamLogin"], "account-name")
        self.assertEqual(config["SteamTradeToken"], "tradetoken")
        self.assertEqual(config["WebProxy"], "socks5://proxy.example:1080")
        self.assertNotIn("SteamProtocols", config)
        self.assertTrue(summary["steam_connection_proxy_requires_global_setting"])
        self.assertNotIn("secret", str(summary))


    def test_global_protocol_change_requires_explicit_confirmation(self):
        with self.assertRaises(ValueError):
            ASFClient().apply_global_steam_protocols()
    
    @patch("integrations.asf.client.urlopen")
    def test_global_protocol_change_preserves_other_settings_and_verifies(self, urlopen):
        original = {
            "IPC": True,
            "SteamProtocols": 7,
            "WebProxy": "http://global-proxy.example:8080",
            "WebProxyUsername": "global-user",
            "Debug": False,
        }
        updated = dict(original, SteamProtocols=4)
        urlopen.side_effect = [
            FakeResponse({"Success": True, "Result": {"GlobalConfig": original}}),
            FakeResponse({"Success": True}),
            FakeResponse({"Success": True, "Result": {"GlobalConfig": updated}}),
        ]
        summary = ASFClient().apply_global_steam_protocols(confirmed_instance_wide_change=True)
        self.assertTrue(summary["saved"])
        self.assertEqual(summary["scope"], "all ASF bots")
        posted = json.loads(urlopen.call_args_list[1].args[0].data.decode("utf-8"))
        config = posted["GlobalConfig"]
        self.assertEqual(config["SteamProtocols"], 4)
        self.assertTrue(config["IPC"])
        self.assertEqual(config["WebProxy"], original["WebProxy"])
        self.assertEqual(config["WebProxyUsername"], original["WebProxyUsername"])
        self.assertFalse(config["Debug"])

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


    @patch("integrations.asf.client.urlopen")
    def test_global_protocol_change_fails_if_readback_does_not_match(self, urlopen):
        original = {"IPC": True, "SteamProtocols": 7}
        unchanged = {"IPC": True, "SteamProtocols": 7}
        urlopen.side_effect = [
            FakeResponse({"Success": True, "Result": {"GlobalConfig": original}}),
            FakeResponse({"Success": True}),
            FakeResponse({"Success": True, "Result": {"GlobalConfig": unchanged}}),
        ]
        with self.assertRaises(ASFError):
            ASFClient().apply_global_steam_protocols(confirmed_instance_wide_change=True)
        self.assertEqual(urlopen.call_count, 3)

    @patch("integrations.asf.client.urlopen")
    def test_global_protocol_change_refuses_missing_setting_without_post(self, urlopen):
        urlopen.return_value = FakeResponse({
            "Success": True, "Result": {"GlobalConfig": {"IPC": True}}
        })
        with self.assertRaises(ASFError):
            ASFClient().apply_global_steam_protocols(confirmed_instance_wide_change=True)
        self.assertEqual(urlopen.call_count, 1)


    @patch("integrations.asf.client.urlopen")
    def test_proxy_apply_fails_if_readback_does_not_match(self, urlopen):
        existing = {
            "AcceptGifts": True,
            "Enabled": True,
            "WebProxy": None,
            "WebProxyUsername": None,
            "WebProxyPassword": None,
        }
        unchanged = {
            "AcceptGifts": True,
            "Enabled": True,
            "WebProxy": None,
            "WebProxyUsername": None,
        }
        urlopen.side_effect = [
            FakeResponse({
                "Success": True,
                "Result": {"bot-a": {"BotConfig": existing, "BotName": "bot-a"}},
            }),
            FakeResponse({"Success": True, "Result": {"bot-a": True}}),
            FakeResponse({
                "Success": True,
                "Result": {"bot-a": {"BotConfig": unchanged, "BotName": "bot-a"}},
            }),
        ]
        with self.assertRaises(ASFError) as raised:
            ASFClient().apply_proxy_settings("bot-a", {
                "WebProxy": "socks5://proxy.example:1080",
                "WebProxyUsername": "user",
                "WebProxyPassword": "secret",
            })
        self.assertIn("не подтвердил адрес веб-прокси", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(urlopen.call_count, 3)

    @patch("integrations.asf.client.urlopen")
    def test_get_bots_returns_bot_map_from_asf_status(self, urlopen):
        urlopen.return_value = FakeResponse({
            "Success": True,
            "Result": {"Bots": {"bot-a": True, "bot-b": False}},
        })
        self.assertEqual(ASFClient().get_bots(), {"bot-a": True, "bot-b": False})
        self.assertEqual(urlopen.call_args.args[0].full_url, "http://127.0.0.1:1242/Api/ASF")

    @patch("integrations.asf.client.urlopen")
    def test_get_bots_rejects_missing_bot_map(self, urlopen):
        urlopen.return_value = FakeResponse({"Success": True, "Result": {"Version": "6.3.10.6"}})
        with self.assertRaises(ASFError):
            ASFClient().get_bots()

    def test_restart_requires_explicit_confirmation(self):
        with self.assertRaises(ValueError):
            ASFClient().restart()

    @patch("integrations.asf.client.urlopen")
    def test_restart_uses_official_local_endpoint(self, urlopen):
        urlopen.return_value = FakeResponse({"Success": True})
        self.assertEqual(ASFClient().restart(confirmed=True), {"Success": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:1242/Api/ASF/Restart")
        self.assertEqual(request.get_method(), "POST")

    def test_build_new_bot_config_defaults_to_disabled_and_has_no_password(self):
        config = ASFClient.build_new_bot_config("new-bot", steam_login=" steam-user ")
        self.assertEqual(config, {"Enabled": False, "SteamLogin": "steam-user"})
        self.assertNotIn("SteamPassword", config)

    def test_build_new_bot_config_rejects_reserved_and_unsafe_names(self):
        for name in ("", "ASF", ".hidden", "../bot", "bot/name", "bot\\\\name", "bot."):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    ASFClient.build_new_bot_config(name)

    def test_serialize_new_bot_config_returns_json_filename_and_content(self):
        filename, content = ASFClient.serialize_new_bot_config("card-bot", steam_login="user")
        self.assertEqual(filename, "card-bot.json")
        self.assertEqual(json.loads(content), {"Enabled": False, "SteamLogin": "user"})
        self.assertTrue(content.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
