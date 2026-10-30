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


if __name__ == "__main__":
    unittest.main()
