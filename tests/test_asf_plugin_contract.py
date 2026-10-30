import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ASFPluginContractTests(unittest.TestCase):
    def test_inventory_api_failure_is_reported_as_pre_send_and_never_as_unknown_trade(self):
        source = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")
        inventory_call = source.index("targetBot.Actions.GetInventory")
        send_call = source.index("targetBot.ArchiWebHandler.SendTradeOffer")
        inventory_region = source[inventory_call - 500:send_call]

        self.assertIn("catch (MissingMethodException", inventory_region)
        self.assertIn("Проверка инвентаря ASF", inventory_region)
        self.assertIn("resultUnknown: false", inventory_region)

    def test_plugin_push_workflow_targets_the_active_feature_branch(self):
        workflow = (ROOT / ".github" / "workflows" / "asf-plugin.yml").read_text(encoding="utf-8")
        self.assertIn("      - feature/asf-bulk-account-import", workflow)
        self.assertNotIn("      - feature/steam-card-set-selection", workflow)

    def test_unexpected_send_dispatch_exception_returns_fail_closed_diagnostic(self):
        source = (ROOT / "integrations" / "asf_plugin" / "GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        send_dispatch = source.index("TradeDelivery.ProcessSendCommand")
        dispatch_region = source[send_dispatch:send_dispatch + 1200]

        self.assertIn("catch (Exception exception)", dispatch_region)
        self.assertIn("resultUnknown = true", dispatch_region)
        self.assertIn("exception.GetType().Name", dispatch_region)

    def test_diagnostic_command_checks_inventory_without_trade_send(self):
        source = (ROOT / "integrations" / "asf_plugin" / "GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        self.assertIn('DiagnosticCommandName = "gamedelivery-diagnose"', source)
        self.assertIn("RunDiagnosticCommand(diagnosticBot)", source)
        self.assertIn("gamedelivery-diagnose <BotName>", source)
        self.assertIn("GetInventory(", source)
        diagnostic_region = source[source.index("RunDiagnosticCommand"):source.index("private static string Failure")]
        self.assertIn("inventoryCount", diagnostic_region)
        self.assertNotIn("SendTradeOffer", diagnostic_region)


if __name__ == "__main__":
    unittest.main()
