import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ASFPluginContractTests(unittest.TestCase):
    def test_inventory_api_failure_is_reported_as_pre_send_and_never_as_unknown_trade(self):
        source = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")
        inventory_call = source.index("targetBot.Actions.GetInventory")
        send_call = source.index("InvokeSendTradeOfferAsync(")
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
        self.assertIn("exceptionType = exception.GetType().FullName", dispatch_region)
        self.assertIn("targetSite = exception.TargetSite?.ToString()", dispatch_region)
        self.assertIn("stackTrace = SafeDiagnosticText(exception.StackTrace)", dispatch_region)

    def test_diagnostic_command_reports_runtime_trade_send_api_signature(self):
        source = (ROOT / "integrations" / "asf_plugin" / "GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        diagnostic_region = source[source.index("RunDiagnosticCommand"):source.index("private static string Failure")]
        self.assertIn("sendTradeOfferMethods", diagnostic_region)
        self.assertIn("ArchiWebHandler", diagnostic_region)
        self.assertIn("GetMethodSignatures", diagnostic_region)
        self.assertIn("type.GetMethods(", source)
        self.assertIn("asfAssembly", diagnostic_region)
        self.assertIn("moduleVersionId", diagnostic_region)
        self.assertIn("pluginReferences", diagnostic_region)
        self.assertIn("confirmationMethods", diagnostic_region)
        self.assertIn("GetAssemblyDiagnostics", diagnostic_region)

    def test_diagnostic_command_checks_inventory_without_trade_send(self):
        source = (ROOT / "integrations" / "asf_plugin" / "GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        self.assertIn('DiagnosticCommandName = "gamedelivery-diagnose"', source)
        self.assertIn("RunDiagnosticCommand(diagnosticBot)", source)
        self.assertIn("gamedelivery-diagnose <BotName>", source)
        self.assertIn("GetInventory(", source)
        diagnostic_region = source[source.index("RunDiagnosticCommand"):source.index("private static string Failure")]
        self.assertIn("inventoryCount", diagnostic_region)
        self.assertNotIn(".SendTradeOffer(", diagnostic_region)

    def test_trade_send_uses_runtime_reflection_instead_of_compile_time_binding(self):
        source = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")
        self.assertIn("InvokeSendTradeOfferAsync(", source)
        self.assertIn('candidate.Name, "SendTradeOffer"', source)
        self.assertIn("method.Invoke(", source)
        self.assertIn("TargetInvocationException", source)
        self.assertNotIn("targetBot.ArchiWebHandler.SendTradeOffer(", source)

    def test_trade_confirmation_uses_runtime_reflection_not_compile_time_binding(self):
        source = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")
        self.assertIn("InvokeTradeConfirmationAsync(", source)
        self.assertIn('"HandleTwoFactorAuthenticationConfirmations"', source)
        self.assertIn("method.Invoke(", source)
        self.assertNotIn("targetBot.Actions.HandleTwoFactorAuthenticationConfirmations(", source)

    def test_send_failure_preserves_missing_method_name_and_stack_for_root_cause_analysis(self):
        source = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")
        send_call = source.index("InvokeSendTradeOfferAsync(")
        catch_region = source[send_call:source.index("finally", send_call)]
        self.assertIn("exception);", catch_region)
        failure_region = source[source.index("private static string UnknownFailure"):source.index("private static string Failure")]
        self.assertIn("exception is MissingMethodException", failure_region)
        self.assertIn("StackTrace", failure_region)
        self.assertIn("exceptionType", failure_region)

    def test_all_plugin_json_options_explicitly_enable_metadata_resolver(self):
        for relative_path in (
            "integrations/asf_plugin/GameDeliveryPlugin.cs",
            "integrations/asf_plugin/TradeDelivery.cs",
        ):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("using System.Text.Json.Serialization.Metadata;", source)
            self.assertIn(
                "TypeInfoResolver = new DefaultJsonTypeInfoResolver()",
                source,
                msg=f"{relative_path} must not depend on the host's reflection default",
            )


if __name__ == "__main__":
    unittest.main()
