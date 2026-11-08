import io
import sqlite3
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from integrations.asf.client import ASFClient, ASFError
from integrations.asf.cs2_inventory_probe import main, probe_cs2_inventory_item


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        import json

        return json.dumps(self.payload).encode("utf-8")


def response(assets, descriptions):
    return {
        "Success": True,
        "Result": {
            "Test Bot": {
                "Assets": assets,
                "Descriptions": descriptions,
            }
        },
    }


class CS2InventoryProbeTests(unittest.TestCase):
    @patch("integrations.asf.client.urlopen")
    def test_probe_refuses_working_port_and_non_loopback_before_network(self, urlopen):
        for ipc_url in (
            "http://127.0.0.1:1242",
            "http://192.168.1.10:1243",
            "http://example.com:1243",
            "https://127.0.0.1:1243",
        ):
            with self.subTest(ipc_url=ipc_url):
                with self.assertRaises(ValueError):
                    probe_cs2_inventory_item(
                        ipc_url=ipc_url,
                        bot_name="Test Bot",
                        asset_id="9001",
                    )
        urlopen.assert_not_called()

    @patch("integrations.asf.client.urlopen")
    @patch("sqlite3.connect", side_effect=AssertionError("SQLite must not be opened"))
    def test_exact_asset_probe_uses_only_get_730_2_and_returns_safe_fields(
        self, sqlite_connect, urlopen
    ):
        urlopen.return_value = FakeResponse(response(
            [
                {"assetid": "9001", "classid": "10", "instanceid": "0", "amount": "2"},
                {"assetid": "90010", "classid": "11", "instanceid": "0"},
                {"assetid": "19001", "classid": "12", "instanceid": "0"},
            ],
            [
                {
                    "classid": "10", "instanceid": "0", "name": "AK-47 | Test",
                    "type": "Consumer Grade Rifle", "market_hash_name": "AK-47 | Test",
                    "tradable": 1,
                },
                {"classid": "11", "instanceid": "0", "name": "Wrong suffix"},
                {"classid": "12", "instanceid": "0", "name": "Wrong prefix"},
            ],
        ))

        with patch.object(ASFClient, "send_trade_offer", side_effect=AssertionError) as send_trade_offer, \
             patch.object(ASFClient, "preview_trade_offer", side_effect=AssertionError) as preview_trade_offer, \
             patch.object(ASFClient, "send_command", side_effect=AssertionError) as send_command:
            item = probe_cs2_inventory_item(
                ipc_url="http://127.0.0.1:1243",
                bot_name="Test Bot",
                asset_id="9001",
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.full_url,
            "http://127.0.0.1:1243/Api/Bot/Test%20Bot/Inventory/730/2?language=english",
        )
        self.assertEqual(item["asset_id"], "9001")
        self.assertEqual(item["class_id"], "10")
        self.assertEqual(item["name"], "AK-47 | Test")
        self.assertEqual(item["amount"], "2")
        self.assertIs(item["tradable"], True)
        self.assertEqual(item["marketable"], "unknown")
        self.assertEqual(item["tradable_after"], "unknown")
        self.assertEqual(set(item), {
            "asset_id", "class_id", "instance_id", "amount", "name", "type",
            "market_hash_name", "tradable", "marketable", "tradable_after",
        })
        sqlite_connect.assert_not_called()
        send_trade_offer.assert_not_called()
        preview_trade_offer.assert_not_called()
        send_command.assert_not_called()

    @patch("integrations.asf.client.urlopen")
    def test_unknown_asset_id_is_not_matched_by_prefix_or_substring(self, urlopen):
        urlopen.return_value = FakeResponse(response(
            [{"assetid": "90010", "classid": "10", "instanceid": "0"}],
            [{"classid": "10", "instanceid": "0", "name": "Near match"}],
        ))

        with self.assertRaisesRegex(ASFError, "9001.*не найден"):
            probe_cs2_inventory_item(
                ipc_url="http://127.0.0.1:1243",
                bot_name="Test Bot",
                asset_id="9001",
            )

    @patch("integrations.asf.client.urlopen")
    def test_missing_item_fields_remain_unknown_and_amount_is_not_invented(self, urlopen):
        urlopen.return_value = FakeResponse(response(
            [{"assetid": "9001"}],
            [],
        ))

        item = probe_cs2_inventory_item(
            ipc_url="http://127.0.0.1:1243",
            bot_name="Test Bot",
            asset_id="9001",
        )

        self.assertEqual(item["asset_id"], "9001")
        for field in (
            "class_id", "instance_id", "amount", "name", "type",
            "market_hash_name", "tradable", "marketable", "tradable_after",
        ):
            with self.subTest(field=field):
                self.assertEqual(item[field], "unknown")

    @patch("integrations.asf.cs2_inventory_probe.getpass.getpass", return_value="secret")
    @patch("integrations.asf.cs2_inventory_probe.probe_cs2_inventory_item", side_effect=ASFError("ASF не смог получить инвентарь"))
    def test_cli_reports_asf_errors_without_echoing_ipc_password(self, probe, getpass):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([
                "--ipc-url", "http://127.0.0.1:1243",
                "--bot-name", "Test Bot",
                "--asset-id", "9001",
            ])

        self.assertEqual(code, 1)
        self.assertIn("ASF не смог получить инвентарь", errors.getvalue())
        self.assertNotIn("secret", output.getvalue() + errors.getvalue())
        probe.assert_called_once()
        getpass.assert_called_once()


if __name__ == "__main__":
    unittest.main()
