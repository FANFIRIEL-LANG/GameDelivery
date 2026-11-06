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
        callback_start = source.index("public async Task<string?> OnBotCommand")
        callback_end = source.index("private static async Task<string> RunDiagnosticCommand", callback_start)
        callback = source[callback_start:callback_end]
        self.assertIn("catch (Exception exception)", callback)
        self.assertIn("resultUnknown = isSend", callback)
        self.assertIn("exception.GetType().Name", callback)
        self.assertIn("exceptionType = exception.GetType().FullName", callback)
        self.assertIn("SafeDiagnosticText(exception.Message)", callback)

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
        self.assertIn("exception,", catch_region)
        self.assertIn("secret.TradeToken", catch_region)
        failure_region = source[source.index("private static string UnknownFailure"):source.index("private static string Failure")]
        self.assertIn("exception is MissingMethodException", failure_region)
        self.assertIn("StackTrace", failure_region)
        self.assertIn("exceptionType", failure_region)
        self.assertIn("tradeToken", failure_region)
        self.assertIn("Replace(tradeToken, \"[REDACTED]\"", source)

    def test_plugin_json_uses_framework_system_text_json_without_newtonsoft(self):
        for relative_path in (
            "integrations/asf_plugin/GameDeliveryPlugin.cs",
            "integrations/asf_plugin/TradeDelivery.cs",
        ):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn("using System.Text.Json;", source)
            self.assertIn("JsonSerializer.Serialize(", source)
            self.assertIn("JsonSerializer.Deserialize<", source)
            self.assertIn("PropertyNamingPolicy = JsonNamingPolicy.CamelCase", source)
            self.assertIn("PropertyNameCaseInsensitive = true", source)
            self.assertNotIn("Newtonsoft", source)
            self.assertNotIn("JsonConvert", source)
            self.assertNotIn("JsonSerializationException", source)

    def test_plugin_deserialization_dtos_have_public_parameterless_constructors(self):
        plugin = (ROOT / "integrations/asf_plugin/GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        trade = (ROOT / "integrations/asf_plugin/TradeDelivery.cs").read_text(encoding="utf-8")
        self.assertIn("public PreviewRequest() { }", plugin)
        self.assertIn("public SendRequest() { }", trade)
        self.assertIn("public PendingTradeSecret() { }", trade)
        self.assertIn("public SendRecord() { }", trade)

    def test_plugin_has_no_external_json_package_and_does_not_package_shared_runtime_dependencies(self):
        project = (ROOT / "integrations" / "asf_plugin" / "GameDelivery.ASFPlugin.csproj").read_text(encoding="utf-8")
        workflow = (ROOT / ".github" / "workflows" / "asf-plugin.yml").read_text(encoding="utf-8")
        self.assertNotIn("Newtonsoft.Json", project)
        self.assertNotIn("PackageReference", project)
        self.assertIn('<Private>false</Private>', project)
        self.assertIn('<CopyLocalLockFileAssemblies>false</CopyLocalLockFileAssemblies>', project)
        self.assertIn("Verify plugin has no Newtonsoft runtime dependency", workflow)
        self.assertIn('Unexpected host-shared dependency in plugin output', workflow)
        self.assertIn('GameDelivery.ASFPlugin.dll', workflow)


    def test_plugin_workflow_uses_a_yaml_block_for_multiline_build_info(self):
        workflow = (ROOT / ".github" / "workflows" / "asf-plugin.yml").read_text(encoding="utf-8")
        self.assertIn("          {\n            printf", workflow)
        self.assertIn("          } > dist/GameDelivery.ASFPlugin/BUILD_INFO.txt", workflow)
        self.assertNotIn("printf \'GameDelivery ASF plugin\\nGameDelivery commit:", workflow)

    def test_plugin_workflow_resolves_nested_asf_archive_directory(self):
        workflow = (ROOT / ".github" / "workflows" / "asf-plugin.yml").read_text(encoding="utf-8")
        self.assertIn('find .asf-bin -type f -name ArchiSteamFarm.dll', workflow)
        self.assertIn('ASF_BIN_DIR=$GITHUB_WORKSPACE/$asf_bin_dir', workflow)
        self.assertNotIn('.asf-bin/ArchiSteamFarm.dll', workflow)

    def test_plugin_build_targets_the_installed_asf_release(self):
        workflow = (ROOT / ".github" / "workflows" / "asf-plugin.yml").read_text(encoding="utf-8")
        readme = (ROOT / "integrations" / "asf_plugin" / "README.md").read_text(encoding="utf-8")
        self.assertIn("6.3.11.7", workflow)
        self.assertIn("sha256sum --check", workflow)
        self.assertIn("cb3ddb9bd10560458a460664c9af6186e124e89801d118197a82eef5a9315ceb", workflow)
        self.assertNotIn("6.3.10.6", workflow)
        self.assertIn("ASF 6.3.11.7", readme)

    def test_preview_and_send_accept_any_tradable_steam_community_item(self):
        plugin = (ROOT / "integrations" / "asf_plugin" / "GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        send = (ROOT / "integrations" / "asf_plugin" / "TradeDelivery.cs").read_text(encoding="utf-8")

        preview_start = plugin.index("foreach (string rawAssetId in request.AssetIds)")
        preview_end = plugin.index("return JsonSerializer.Serialize(new PreviewResponse", preview_start)
        preview_validation = plugin[preview_start:preview_end]
        send_start = send.index("foreach (string rawAssetId in request.AssetIds)")
        send_end = send.index("SendRecord record = new()", send_start)
        send_validation = send[send_start:send_end]

        for validation in (preview_validation, send_validation):
            self.assertIn("asset.AppID != Asset.SteamAppID", validation)
            self.assertIn("asset.ContextID != Asset.SteamCommunityContextID", validation)
            self.assertIn("!asset.Tradable", validation)
            self.assertNotIn("TradingCard", validation)


    def test_plugin_normalizes_custom_command_from_args_and_raw_message(self):
        source = (ROOT / "integrations/asf_plugin/GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        parser = (ROOT / "integrations/asf_plugin/CommandArgumentParser.cs").read_text(encoding="utf-8")
        self.assertIn("CommandArgumentParser.TryNormalize", source)
        self.assertIn("message", parser)
        self.assertIn("args", parser)
        self.assertIn("commandPrefix", parser)
        self.assertIn("DiagnosticCommandName", parser)
        self.assertIn("PreviewCommandName", parser)
        self.assertIn("SendCommandName", parser)

    def test_plugin_verifies_command_handler_registration_during_asf_initialization(self):
        source = (ROOT / "integrations/asf_plugin/GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        self.assertIn("class GameDeliveryPlugin : IASF, IBotCommand2", source)
        self.assertIn("OnASFInit(", source)
        self.assertIn("ActivePlugins", source)
        self.assertIn("IBotCommand2", source)
        self.assertIn("LogGenericInfo", source)

    def test_command_callback_contains_all_exceptions_and_emits_safe_entry_log(self):
        source = (ROOT / "integrations/asf_plugin/GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        start = source.index("public async Task<string?> OnBotCommand")
        end = source.index("private static async Task<string> RunDiagnosticCommand", start)
        callback = source[start:end]
        self.assertIn("try", callback)
        self.assertIn("catch (Exception exception)", callback)
        self.assertIn("LogGenericInfo", callback)
        self.assertIn("argumentCount", callback)
        self.assertNotIn("args[1]", callback[callback.index("LogGenericInfo"):callback.index("try")])

    def test_send_lock_covers_idempotency_inventory_preflight_and_external_send(self):
        source = (ROOT / "integrations/asf_plugin/TradeDelivery.cs").read_text(encoding="utf-8")
        start = source.index("public static async Task<string> ProcessSendCommand")
        end = source.index("private static string GetPendingSecretPath", start)
        send = source[start:end]
        lock_at = send.index("SendSemaphore.WaitAsync")
        records_at = send.index("LoadSendRecords()")
        duplicate_at = send.index("records.TryGetValue")
        inventory_at = send.index("targetBot.Actions.GetInventory")
        offer_at = send.index("targetBot.ArchiWebHandler.SendTradeOffer(")
        self.assertLess(lock_at, records_at)
        self.assertLess(records_at, duplicate_at)
        self.assertLess(duplicate_at, inventory_at)
        self.assertLess(inventory_at, offer_at)
        self.assertIn("SendSemaphore.Release()", send)

    def test_preview_and_send_reject_noncanonical_numeric_asset_ids(self):
        plugin = (ROOT / "integrations/asf_plugin/GameDeliveryPlugin.cs").read_text(encoding="utf-8")
        send = (ROOT / "integrations/asf_plugin/TradeDelivery.cs").read_text(encoding="utf-8")
        self.assertIn("id.ToString(CultureInfo.InvariantCulture)", plugin)
        self.assertIn("id.ToString(CultureInfo.InvariantCulture)", send)
        self.assertIn("StringComparison.Ordinal", plugin)
        self.assertIn("StringComparison.Ordinal", send)

    def test_pending_trade_secret_must_be_deleted_before_external_send(self):
        source = (ROOT / "integrations/asf_plugin/TradeDelivery.cs").read_text(encoding="utf-8")
        start = source.index("public static async Task<string> ProcessSendCommand")
        end = source.index("private static string GetPendingSecretPath", start)
        send = source[start:end]
        delete_at = send.index("File.Delete(secretPath)")
        offer_at = send.index("targetBot.ArchiWebHandler.SendTradeOffer(")
        self.assertLess(delete_at, offer_at)
        self.assertIn("Не удалось удалить одноразовый секрет", send)
        self.assertNotIn("catch (IOException) { } catch (UnauthorizedAccessException) { }", send[:offer_at])

    def test_plugin_assembly_version_is_incremented_for_fixed_dispatch_artifact(self):
        project = (ROOT / "integrations/asf_plugin/GameDelivery.ASFPlugin.csproj").read_text(encoding="utf-8")
        self.assertIn("<Version>1.0.1</Version>", project)


if __name__ == "__main__":
    unittest.main()
