import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ASFPluginContractTests(unittest.TestCase):
    def test_inventory_api_failure_is_reported_as_pre_send_and_never_as_unknown_trade(self):
        source = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")
        inventory_call = source.index("targetBot.Actions.GetInventory")
        send_call = source.index("targetBot.ArchiWebHandler.SendTradeOffer(")
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

    def test_trade_send_uses_the_public_asf_api_directly(self):
        source = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")
        self.assertIn("targetBot.ArchiWebHandler.SendTradeOffer(", source)
        self.assertIn("targetBot.Actions.HandleTwoFactorAuthenticationConfirmations(", source)
        self.assertNotIn("InvokeSendTradeOfferAsync", source)
        self.assertNotIn("InvokeTradeConfirmationAsync", source)
        self.assertNotIn("method.Invoke(", source)
        self.assertNotIn("System.Reflection", source)

    def test_send_failure_preserves_missing_method_name_and_stack_for_root_cause_analysis(self):
        source = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")
        send_call = source.index("targetBot.ArchiWebHandler.SendTradeOffer(")
        catch_region = source[send_call:source.index("finally", send_call)]
        self.assertIn("exception);", catch_region)
        failure_region = source[source.index("private static string UnknownFailure"):source.index("private static string Failure")]
        self.assertIn("exception is MissingMethodException", failure_region)
        self.assertIn("StackTrace", failure_region)
        self.assertIn("exceptionType", failure_region)

    def test_plugin_json_uses_newtonsoft_without_system_text_json_reflection_metadata(self):
        for relative_path in (
            "integrations/asf_plugin/GameDeliveryPlugin.cs",
            "integrations/asf_plugin/TradeDelivery.cs",
        ):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("using Newtonsoft.Json;", source)
            self.assertIn("JsonConvert.SerializeObject(", source)
            self.assertNotIn("System.Text.Json", source)
            self.assertNotIn("DefaultJsonTypeInfoResolver", source)

    def test_plugin_uses_host_newtonsoft_and_does_not_package_shared_runtime_dependencies(self):
        project = (ROOT / "integrations" / "asf_plugin" / "GameDelivery.ASFPlugin.csproj").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "asf-plugin.yml").read_text(encoding="utf-8")
        self.assertIn('<Reference Include="Newtonsoft.Json">', project)
        self.assertIn('<HintPath>$(ASF_BIN_DIR)/Newtonsoft.Json.dll</HintPath>', project)
        self.assertIn('<Private>false</Private>', project)
        self.assertIn('<CopyLocalLockFileAssemblies>false</CopyLocalLockFileAssemblies>', project)
        self.assertNotIn('PackageReference Include="Newtonsoft.Json"', project)
        self.assertIn('test -s .asf-bin/Newtonsoft.Json.dll', workflow)
        self.assertIn('Unexpected host-shared dependency in plugin output', workflow)
        self.assertIn('GameDelivery.ASFPlugin.dll', workflow)
        self.assertNotIn('json_dll=', workflow)
        self.assertNotIn('cp "$json_dll"', workflow)



    def test_plugin_build_targets_the_installed_asf_release(self):
        workflow = (ROOT / ".github" / "workflows" / "asf-plugin.yml").read_text(encoding="utf-8")
        readme = (ROOT / "integrations" / "asf_plugin" / "README.md").read_text(encoding="utf-8")
        self.assertIn("6.3.11.7", workflow)
        self.assertIn("SHA512SUMS", workflow)
        self.assertNotIn("6.3.10.6", workflow)
        self.assertIn("ASF 6.3.11.7", readme)

    def test_preview_and_send_accept_any_tradable_steam_community_item(self):
        plugin = (ROOT / "integrations" / "asf_plugin" / "GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        send = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")

        preview_start = plugin.index("foreach (string rawAssetId in request.AssetIds)")
        preview_end = plugin.index("return JsonConvert.SerializeObject(new PreviewResponse", preview_start)
        preview_validation = plugin[preview_start:preview_end]
        send_start = send.index("foreach (string rawAssetId in request.AssetIds)")
        send_end = send.index("string fingerprint = ComputeFingerprint(request)", send_start)
        send_validation = send[send_start:send_end]

        for validation in (preview_validation, send_validation):
            self.assertIn("asset.AppID != Asset.SteamAppID", validation)
            self.assertIn("asset.ContextID != Asset.SteamCommunityContextID", validation)
            self.assertIn("!asset.Tradable", validation)
            self.assertNotIn("TradingCard", validation)


if __name__ == "__main__":
    unittest.main()
