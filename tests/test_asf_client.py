import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
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

    @patch.object(ASFClient, "request")
    def test_preview_trade_offer_uses_plugin_and_does_not_send_trade_token(self, request):
        request.return_value = {
            "Success": True,
            "Result": json.dumps({
                "success": True,
                "mode": "preview-only",
                "sendEnabled": False,
                "orderId": "GD-ABC",
                "botName": "Bot A",
                "targetSteamId64": 76561197960389184,
                "assetCount": 2,
                "assets": [
                    {"assetId": "100", "name": "Card A", "tradable": True},
                    {"assetId": "200", "name": "Card B", "tradable": True},
                ],
            }),
        }

        preview = ASFClient().preview_trade_offer(
            order_id="GD-ABC",
            bot_name="Bot A",
            trade_url="https://steamcommunity.com/tradeoffer/new/?partner=123456&token=private-trade-token",
            asset_ids=["100", "200"],
        )

        request.assert_called_once()
        path = request.call_args.args[0]
        kwargs = request.call_args.kwargs
        self.assertEqual(path, "/Api/Command")
        self.assertEqual(kwargs["method"], "POST")
        command = kwargs["body"]["Command"]
        self.assertTrue(command.startswith("gamedelivery-preview "))
        self.assertNotIn("private-trade-token", command)
        import base64
        encoded = command.split(" ", 1)[1]
        decoded = base64.urlsafe_b64decode(encoded + "=" * ((4 - len(encoded) % 4) % 4))
        payload = json.loads(decoded.decode("utf-8"))
        self.assertEqual(payload["OrderId"], "GD-ABC")
        self.assertEqual(payload["BotName"], "Bot A")
        self.assertEqual(payload["TargetSteamId64"], 76561197960389184)
        self.assertEqual(payload["AssetIds"], ["100", "200"])
        self.assertEqual(preview["assetCount"], 2)
        self.assertFalse(preview["sendEnabled"])

    @patch.object(ASFClient, "request")
    def test_preview_trade_offer_fails_closed_if_plugin_reply_enables_sending(self, request):
        request.return_value = {
            "Success": True,
            "Result": json.dumps({
                "success": True,
                "mode": "preview-only",
                "sendEnabled": True,
                "orderId": "GD-ABC",
                "botName": "Bot A",
                "targetSteamId64": 76561197960389184,
                "assets": [{"assetId": "100"}],
            }),
        }
        with self.assertRaisesRegex(ASFError, "некорректное подтверждение"):
            ASFClient().preview_trade_offer(
                order_id="GD-ABC",
                bot_name="Bot A",
                trade_url="https://steamcommunity.com/tradeoffer/new/?partner=123456&token=abc",
                asset_ids=["100"],
            )

    def test_preview_trade_offer_rejects_duplicate_assets_and_bad_trade_urls(self):
        client = ASFClient()
        with self.assertRaisesRegex(ValueError, "повторяющиеся"):
            client.preview_trade_offer(
                order_id="GD-ABC",
                bot_name="Bot A",
                trade_url="https://steamcommunity.com/tradeoffer/new/?partner=123456&token=abc",
                asset_ids=["100", "100"],
            )
        with self.assertRaises(ValueError):
            client.preview_trade_offer(
                order_id="GD-ABC",
                bot_name="Bot A",
                trade_url="https://evil.example/tradeoffer/new/?partner=123456&token=abc",
                asset_ids=["100"],
            )

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
            "http://localhost",
            "http://127.0.0.1",
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
    def test_get_bot_inventory_uses_full_inventory_route_and_language(self, urlopen):
        payload = {
            "Success": True,
            "Result": {
                "bot-a": {
                    "Assets": [{"assetid": "asset-1", "classid": "class-1", "instanceid": "0"}],
                    "Descriptions": [{
                        "classid": "class-1",
                        "instanceid": "0",
                        "name": "Trading Card",
                        "type": "Trading Card",
                    }],
                }
            },
        }
        urlopen.return_value = FakeResponse(payload)

        result = ASFClient().get_bot_inventory(
            "bot-a", app_id=753, context_id=6, language="english"
        )

        self.assertEqual(result, payload)
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:1242/Api/Bot/bot-a/Inventory/753/6?language=english",
        )
        self.assertEqual(request.get_method(), "GET")

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

    @patch.object(ASFClient, "send_command")
    def test_start_bot_sends_official_start_command(self, send_command):
        send_command.return_value = {"Success": True, "Result": "OK"}
        result = ASFClient().start_bot("bot-a")
        send_command.assert_called_once_with("start bot-a")
        self.assertEqual(result, {"bot_name": "bot-a", "command_sent": True})

    def test_start_bot_rejects_command_injection_and_ambiguous_names(self):
        for name in ("", "bot-a" + chr(10) + "start all", "bot-a,bot-b"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    ASFClient().start_bot(name)

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

    def test_send_trade_offer_keeps_token_out_of_ipc_command_and_deletes_one_use_secret(self):
        with tempfile.TemporaryDirectory() as temp:
            def fake_send_command(command):
                self.assertTrue(command.startswith("gamedelivery-send "))
                encoded = command.split(" ", 1)[1]
                payload_bytes = base64.urlsafe_b64decode(encoded + "=" * ((4 - len(encoded) % 4) % 4))
                payload = json.loads(payload_bytes.decode("utf-8"))
                self.assertEqual(payload["OrderId"], "GD-SEND-1")
                self.assertEqual(payload["BotName"], "card-bot")
                self.assertEqual(payload["AssetIds"], ["123"])
                self.assertNotIn("TradeToken", payload)
                self.assertNotIn("token", payload)
                self.assertRegex(payload["CredentialNonce"], r"^[a-f0-9]{64}$")

                secret_path = Path(temp) / "GameDelivery" / "pending_trade_requests" / (
                    payload["CredentialNonce"] + ".json"
                )
                secret = json.loads(secret_path.read_text(encoding="utf-8"))
                self.assertEqual(secret["TradeToken"], "Tok-abc_1")
                self.assertEqual(secret["TargetSteamId64"], 76561197960265728 + 12345)
                self.assertEqual(secret["AssetIds"], ["123"])
                return {
                    "Success": True,
                    "Result": json.dumps({
                        "success": True,
                        "mode": "sent",
                        "sendEnabled": True,
                        "orderId": "GD-SEND-1",
                        "botName": "card-bot",
                        "targetSteamId64": 76561197960265728 + 12345,
                        "assetCount": 1,
                        "assets": [{"assetId": "123"}],
                        "offerIds": ["456789"],
                        "confirmationStatus": "handled",
                        "tradeAccepted": False,
                    }),
                }

            with patch.dict(os.environ, {"LOCALAPPDATA": temp}):
                with patch.object(ASFClient, "send_command", side_effect=fake_send_command) as send_command:
                    result = ASFClient().send_trade_offer(
                        order_id="GD-SEND-1",
                        bot_name="card-bot",
                        trade_url="https://steamcommunity.com/tradeoffer/new/?partner=12345&token=Tok-abc_1",
                        asset_ids=["123"],
                    )
            send_command.assert_called_once()
            self.assertTrue(result["success"])
            self.assertEqual(result["offerIds"], ["456789"])
            secret_dir = Path(temp) / "GameDelivery" / "pending_trade_requests"
            self.assertEqual(list(secret_dir.iterdir()), [])

    def test_send_trade_offer_rejects_invalid_trade_url_before_dispatch(self):
        with patch.object(ASFClient, "send_command") as send_command:
            with self.assertRaises(ValueError):
                ASFClient().send_trade_offer(
                    order_id="GD-SEND-2",
                    bot_name="card-bot",
                    trade_url="https://example.com/tradeoffer/new/?partner=12345&token=Tok-abc_1",
                    asset_ids=["123"],
                )
            send_command.assert_not_called()

    @patch.object(ASFClient, "send_command", return_value={"Success": True, "Result": "OK"})
    def test_exit_uses_documented_owner_command(self, send_command):
        self.assertEqual(ASFClient().exit(), {"Success": True, "Result": "OK"})
        send_command.assert_called_once_with("exit")

    def test_build_new_bot_config_defaults_to_disabled_and_has_no_password(self):
        config = ASFClient.build_new_bot_config("new-bot", steam_login=" steam-user ")
        self.assertEqual(config, {"Enabled": False, "SteamLogin": "steam-user"})
        self.assertNotIn("SteamPassword", config)

    def test_build_new_bot_config_rejects_reserved_and_unsafe_names(self):
        for name in ("", "ASF", ".hidden", "../bot", "bot/name", "bot\\name", "bot."):
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
