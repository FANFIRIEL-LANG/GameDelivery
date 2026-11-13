using System;
using System.Collections;
using System.Collections.Generic;
using System.Composition;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using System.Text;
using System.Threading.Tasks;
using System.Reflection;
using System.Runtime.CompilerServices;
using ArchiSteamFarm.Core;
using ArchiSteamFarm.Plugins.Interfaces;
using ArchiSteamFarm.Steam;
using ArchiSteamFarm.Steam.Data;
using ArchiSteamFarm.Steam.Storage;
using ArchiSteamFarm.Storage;
using SteamKit2;

namespace GameDelivery.ASFPlugin;

[Export(typeof(IPlugin))]
public sealed class GameDeliveryPlugin : IASF, IBotCommand2
{
    private const string PreviewCommandName = "gamedelivery-preview";
    private const string SendCommandName = "gamedelivery-send";
    private const string DiagnosticCommandName = "gamedelivery-diagnose";
    private const string TradeStatusCommandName = "gamedelivery-status";
    private const int MaxItemsPerPreview = 255;
    private const int MaxDiagnosticAppIds = 20;
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = true,
        TypeInfoResolver = new DefaultJsonTypeInfoResolver()
    };

    [JsonInclude]
    public string Name => nameof(GameDeliveryPlugin);

    [JsonInclude]
    public Version Version => typeof(GameDeliveryPlugin).Assembly.GetName().Version
        ?? new Version(1, 0, 2, 0);

    public Task OnLoaded() => Task.CompletedTask;

    public Task OnASFInit(IReadOnlyDictionary<string, JsonElement>? additionalConfigProperties = null)
    {
        if (string.Equals(
            Environment.GetEnvironmentVariable("GAMEDELIVERY_VALIDATE_SEND_JIT"),
            "1",
            StringComparison.Ordinal))
        {
            string probe = RunSendStateMachineJitProbe();
            ASF.ArchiLogger.LogGenericInfo("GameDeliveryPlugin send state-machine JIT probe: " + probe);
        }

        try
        {
            Type? pluginsCore = typeof(Bot).Assembly.GetType(
                "ArchiSteamFarm.Plugins.PluginsCore",
                throwOnError: false);
            PropertyInfo? activePluginsProperty = pluginsCore?.GetProperty(
                "ActivePlugins",
                BindingFlags.Static | BindingFlags.NonPublic);
            IEnumerable? activePlugins = activePluginsProperty?.GetValue(null) as IEnumerable;

            if (activePlugins is null)
            {
                ASF.ArchiLogger.LogGenericWarning(
                    "GameDeliveryPlugin: не удалось проверить регистрацию IBotCommand2 в ASF.");
                return Task.CompletedTask;
            }

            int activePluginCount = 0;
            int commandHandlerCount = 0;
            bool thisPluginRegistered = false;

            foreach (object? activePlugin in activePlugins)
            {
                if (activePlugin is null)
                {
                    continue;
                }

                activePluginCount++;
                if (activePlugin is IBotCommand2)
                {
                    commandHandlerCount++;
                }

                if (ReferenceEquals(activePlugin, this))
                {
                    thisPluginRegistered = true;
                }
            }

            string registration = "GameDeliveryPlugin ASF registration: "
                + "activePlugins=" + activePluginCount.ToString(CultureInfo.InvariantCulture)
                + "; commandHandlers=" + commandHandlerCount.ToString(CultureInfo.InvariantCulture)
                + "; thisIsIBotCommand2=" + (this is IBotCommand2).ToString()
                + "; thisIsRegistered=" + thisPluginRegistered.ToString() + ".";

            if (thisPluginRegistered && commandHandlerCount > 0)
            {
                ASF.ArchiLogger.LogGenericInfo(registration);
            }
            else
            {
                ASF.ArchiLogger.LogGenericWarning(registration);
            }
        }
        catch (Exception exception)
        {
            ASF.ArchiLogger.LogGenericWarning(
                "GameDeliveryPlugin: ошибка проверки регистрации обработчика команд: "
                + exception.GetType().Name + ": " + SafeMessage(exception.Message));
        }

        return Task.CompletedTask;
    }

    private static string RunSendStateMachineJitProbe()
    {
        try
        {
            MethodInfo processSendMethod = typeof(TradeDelivery).GetMethod(
                "ProcessSendCommand",
                BindingFlags.Public | BindingFlags.Static)
                ?? throw new MissingMethodException(typeof(TradeDelivery).FullName, "ProcessSendCommand");

            AsyncStateMachineAttribute? asyncStateMachine = processSendMethod
                .GetCustomAttribute<AsyncStateMachineAttribute>();
            Type stateMachineType = asyncStateMachine?.StateMachineType
                ?? throw new InvalidOperationException("ProcessSendCommand async state machine metadata was not found");

            MethodInfo moveNext = stateMachineType.GetMethod(
                "MoveNext",
                BindingFlags.Instance | BindingFlags.NonPublic)
                ?? throw new MissingMethodException(stateMachineType.FullName, "MoveNext");

            RuntimeHelpers.PrepareMethod(moveNext.MethodHandle);
            return JsonSerializer.Serialize(new
            {
                status = "success",
                stateMachineType = stateMachineType.FullName,
                method = moveNext.Name
            }, JsonOptions);
        }
        catch (Exception exception)
        {
            MissingMemberException? missingMember = FindMissingMemberException(exception);
            string? memberName = missingMember is null
                ? null
                : GetMissingMemberField(missingMember, "MemberName") as string;
            string? className = missingMember is null
                ? null
                : GetMissingMemberField(missingMember, "ClassName") as string;
            byte[]? signature = missingMember is null
                ? null
                : GetMissingMemberField(missingMember, "Signature") as byte[];

            return JsonSerializer.Serialize(new
            {
                status = "failed",
                exceptionType = exception.GetType().FullName,
                exceptionMessage = SafeDiagnosticText(exception.ToString()),
                missingMemberName = memberName,
                missingClassName = className,
                missingSignature = signature is { Length: > 0 } ? Convert.ToHexString(signature) : null
            }, JsonOptions);
        }
    }

    public async Task<string?> OnBotCommand(
        Bot bot,
        EAccess access,
        string message,
        string[] args,
        ulong steamID = 0)
    {
        bool isSend = false;
        bool sendDispatchStarted = false;
        string? recognizedCommand = null;

        try
        {
            if (!CommandArgumentParser.TryNormalize(
                message,
                args,
                ASF.GlobalConfig?.CommandPrefix,
                out string[] commandArgs))
            {
                return null;
            }

            recognizedCommand = commandArgs[0];
            bool isPreview = string.Equals(recognizedCommand, PreviewCommandName, StringComparison.OrdinalIgnoreCase);
            isSend = string.Equals(recognizedCommand, SendCommandName, StringComparison.OrdinalIgnoreCase);
            bool isDiagnostic = string.Equals(recognizedCommand, DiagnosticCommandName, StringComparison.OrdinalIgnoreCase);
            bool isTradeStatus = string.Equals(recognizedCommand, TradeStatusCommandName, StringComparison.OrdinalIgnoreCase);
            if (!isPreview && !isSend && !isDiagnostic && !isTradeStatus)
            {
                return null;
            }

            ASF.ArchiLogger.LogGenericInfo(
                "GameDeliveryPlugin.OnBotCommand: command=" + recognizedCommand
                + "; argumentCount=" + (commandArgs.Length - 1).ToString(CultureInfo.InvariantCulture)
                + "; ipcContextBot=" + SafeMessage(bot.BotName) + ".");

            if (access != EAccess.Owner)
            {
                return isDiagnostic
                    ? DiagnosticFailure("Команда доступна только ASF Owner.")
                    : Failure("Команда доступна только ASF Owner.");
            }

            if (isTradeStatus)
            {
                if (commandArgs.Length != 3)
                {
                    return TradeDelivery.TradeStatusFailure(
                        "Использование: gamedelivery-status <BotName> <TradeOfferID>.");
                }

                return await TradeDelivery.ProcessTradeStatusCommand(
                    commandArgs[1],
                    commandArgs[2]).ConfigureAwait(false);
            }

            if (isDiagnostic)
            {
                if (commandArgs.Length < 2 || string.IsNullOrWhiteSpace(commandArgs[1]))
                {
                    return DiagnosticFailure(
                        "Укажи имя бота: gamedelivery-diagnose <BotName> [AppID ...] [--recipient <SteamID64>].");
                }

                Bot? diagnosticBot = Bot.BotsReadOnly?.Values.FirstOrDefault(candidate =>
                    string.Equals(candidate.BotName, commandArgs[1], StringComparison.OrdinalIgnoreCase));
                if (diagnosticBot is null)
                {
                    return DiagnosticFailure("Выбранный бот не найден в текущем экземпляре ASF.");
                }

                if (commandArgs.Length == 2)
                {
                    return await RunDiagnosticCommand(diagnosticBot).ConfigureAwait(false);
                }

                if (!TryParseDiagnosticArguments(
                    commandArgs,
                    out uint[] diagnosticAppIds,
                    out ulong? recipientSteamId64,
                    out string diagnosticArgumentError))
                {
                    return DiagnosticFailure(diagnosticArgumentError);
                }

                return await RunBadgeDiagnosticCommand(
                    diagnosticBot,
                    diagnosticAppIds,
                    recipientSteamId64).ConfigureAwait(false);
            }

            if (commandArgs.Length != 2 || commandArgs[1].Length > 16000)
            {
                return Failure("Некорректный запрос GameDelivery.");
            }

            if (isSend)
            {
                return await TradeDelivery.ProcessSendCommand(
                    commandArgs[1],
                    bot.BotName,
                    () => sendDispatchStarted = true).ConfigureAwait(false);
            }

            PreviewRequest? request;
            try
            {
                string encoded = commandArgs[1].Replace('-', '+').Replace('_', '/');
                encoded = encoded.PadRight(encoded.Length + ((4 - encoded.Length % 4) % 4), '=');
                byte[] bytes = Convert.FromBase64String(encoded);
                request = JsonSerializer.Deserialize<PreviewRequest>(Encoding.UTF8.GetString(bytes), JsonOptions);
            }
            catch (Exception exception) when (exception is FormatException or JsonException)
            {
                return Failure("Не удалось разобрать запрос черновика GameDelivery.");
            }

            if (request is null
                || string.IsNullOrWhiteSpace(request.OrderId)
                || string.IsNullOrWhiteSpace(request.BotName)
                || request.TargetSteamId64 == 0
                || request.AssetIds is null
                || request.AssetIds.Count is < 1 or > MaxItemsPerPreview)
            {
                return Failure("В запросе черновика отсутствуют обязательные поля.");
            }

            if (!TradeDelivery.IsSupportedInventoryContext(request.AppId, request.ContextId))
            {
                return Failure("Поддерживаются только Steam Community 753/6 и CS2 730/2.");
            }

            if (request.AssetIds.Any(string.IsNullOrWhiteSpace)
                || request.AssetIds.Distinct(StringComparer.Ordinal).Count() != request.AssetIds.Count)
            {
                return Failure("Список asset ID пустой или содержит повторы.");
            }

            if (request.AssetIds.Any(value =>
                    !ulong.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out ulong id)
                    || id == 0
                    || !string.Equals(value, id.ToString(CultureInfo.InvariantCulture), StringComparison.Ordinal)))
            {
                return Failure("Список asset ID содержит некорректные или неканонические значения.");
            }

            SteamID recipient = new(request.TargetSteamId64);
            if (!recipient.IsIndividualAccount)
            {
                return Failure("Получатель не является индивидуальным Steam-аккаунтом.");
            }

            Bot? targetBot = Bot.BotsReadOnly?.Values.FirstOrDefault(candidate =>
                string.Equals(candidate.BotName, request.BotName, StringComparison.OrdinalIgnoreCase));
            if (targetBot is null)
            {
                return Failure("Выбранный бот не найден в текущем экземпляре ASF.");
            }

            if (!targetBot.IsConnectedAndLoggedOn)
            {
                return Failure("Выбранный бот ASF не подключён к Steam.");
            }

            (HashSet<Asset>? inventory, string inventoryMessage) = await targetBot.Actions.GetInventory(
                request.AppId,
                request.ContextId,
                language: "english").ConfigureAwait(false);

            if (inventory is null)
            {
                return Failure("Не удалось проверить актуальный инвентарь ASF: " + SafeMessage(inventoryMessage));
            }

            Dictionary<ulong, Asset> byAssetId = inventory
                .Where(asset => asset.AssetID > 0)
                .GroupBy(asset => asset.AssetID)
                .ToDictionary(group => group.Key, group => group.First());

            List<PreviewAsset> previewAssets = new(request.AssetIds.Count);
            foreach (string rawAssetId in request.AssetIds)
            {
                ulong assetId = ulong.Parse(rawAssetId, NumberStyles.None, CultureInfo.InvariantCulture);
                if (!byAssetId.TryGetValue(assetId, out Asset? asset))
                {
                    return Failure("Актив " + rawAssetId + " не найден в актуальном инвентаре выбранного бота.");
                }

                if (!TradeDelivery.IsAssetEligible(
                    asset.AppID,
                    asset.ContextID,
                    asset.Tradable,
                    asset.Description is not null,
                    TradeDelivery.IsNormalTradingCard(asset),
                    request.AppId,
                    request.ContextId))
                {
                    return Failure(
                        "Актив " + rawAssetId + " не соответствует выбранной игре/контексту, "
                        + "не имеет подтверждённого описания, не передаваем или не является обычной Steam Trading Card.");
                }

                previewAssets.Add(new PreviewAsset
                {
                    AssetId = asset.AssetID.ToString(CultureInfo.InvariantCulture),
                    Name = asset.Description?.Name ?? "Steam Community item",
                    ClassId = asset.ClassID.ToString(CultureInfo.InvariantCulture),
                    InstanceId = asset.InstanceID.ToString(CultureInfo.InvariantCulture),
                    Tradable = true
                });
            }

            return JsonSerializer.Serialize(new PreviewResponse
            {
                Success = true,
                Mode = "preview-only",
                SendEnabled = false,
                OrderId = request.OrderId.Trim(),
                BotName = targetBot.BotName,
                TargetSteamId64 = request.TargetSteamId64,
                AppId = request.AppId,
                ContextId = request.ContextId,
                AssetCount = previewAssets.Count,
                Assets = previewAssets,
                Message = "ASF подтвердил состав черновика. Торговое предложение не создавалось и не отправлялось."
            }, JsonOptions);
        }
        catch (Exception exception)
        {
            if (string.Equals(recognizedCommand, DiagnosticCommandName, StringComparison.OrdinalIgnoreCase))
            {
                ASF.ArchiLogger.LogGenericError(
                    "GameDeliveryPlugin diagnostic read failed: " + exception.GetType().Name + ".");
                return DiagnosticFailure("ASF read-only diagnostic failed (" + exception.GetType().Name + ").");
            }

            string safeMessage = SafeDiagnosticText(exception.Message);
            string safeDetails = SafeDiagnosticText(exception.ToString());
            MissingMemberException? missingMember = FindMissingMemberException(exception);
            string? missingMemberName = missingMember is null
                ? null
                : GetMissingMemberField(missingMember, "MemberName") as string;
            string? missingClassName = missingMember is null
                ? null
                : GetMissingMemberField(missingMember, "ClassName") as string;
            byte[]? signatureBytes = missingMember is null
                ? null
                : GetMissingMemberField(missingMember, "Signature") as byte[];
            string? missingSignature = signatureBytes is { Length: > 0 }
                ? Convert.ToHexString(signatureBytes)
                : null;

            ASF.ArchiLogger.LogGenericError(
                "GameDeliveryPlugin.OnBotCommand failed: command="
                + (recognizedCommand ?? "unresolved")
                + "; exception=" + exception.GetType().Name
                + "; ipcContextBot=" + SafeMessage(bot.BotName)
                + "; missingMemberName=" + SafeMessage(missingMemberName ?? "not-available")
                + "; missingClassName=" + SafeMessage(missingClassName ?? "not-available")
                + "; missingSignature=" + SafeMessage(missingSignature ?? "not-available")
                + "; details=" + safeDetails);

            return JsonSerializer.Serialize(new
            {
                success = false,
                mode = isSend ? "send" : "plugin-command",
                sendEnabled = isSend,
                resultUnknown = isSend && sendDispatchStarted,
                error = isSend
                    ? (sendDispatchStarted
                        ? "ASF прервал обработку после начала отправки. Результат требует ручной сверки; автоматический повтор запрещён."
                        : "ASF завершил обработку до запуска обработчика отправки. Торговое предложение не подтверждено; проверь исходящие предложения Steam перед любым повтором.")
                    : "Ошибка обработчика GameDelivery ASF: " + exception.GetType().Name + ": " + safeMessage,
                exceptionType = exception.GetType().FullName,
                exceptionMessage = safeMessage,
                exceptionDetails = safeDetails,
                missingMemberName,
                missingClassName,
                missingSignature
            }, JsonOptions);
        }
    }

    private static MissingMemberException? FindMissingMemberException(Exception exception)
    {
        for (Exception? current = exception; current is not null; current = current.InnerException)
        {
            if (current is MissingMemberException missingMember)
            {
                return missingMember;
            }
        }

        return null;
    }

    private static object? GetMissingMemberField(MissingMemberException exception, string fieldName)
    {
        FieldInfo? field = typeof(MissingMemberException).GetField(
            fieldName,
            BindingFlags.Instance | BindingFlags.NonPublic);
        return field?.GetValue(exception);
    }

    private static async Task<string> RunDiagnosticCommand(Bot bot)
    {
        System.Reflection.Assembly pluginAssembly = typeof(GameDeliveryPlugin).Assembly;
        System.Reflection.Assembly asfAssembly = typeof(Bot).Assembly;
        System.Reflection.Assembly steamKitAssembly = typeof(SteamID).Assembly;
        object actions = bot.Actions;
        object webHandler = bot.ArchiWebHandler;

        string inventoryMethods = GetMethodSignatures(actions.GetType(), "GetInventory");
        string confirmationMethods = GetMethodSignatures(
            actions.GetType(), "HandleTwoFactorAuthenticationConfirmations");
        string sendTradeOfferMethods = GetMethodSignatures(webHandler.GetType(), "SendTradeOffer");
        object runtimeTrimming = GetRuntimeTrimmingDiagnostics();
        string[] pluginReferences = pluginAssembly.GetReferencedAssemblies()
            .Select(reference => reference.FullName ?? reference.Name ?? "unknown")
            .Where(name => name.Contains("ArchiSteamFarm", StringComparison.OrdinalIgnoreCase)
                || name.Contains("SteamKit", StringComparison.OrdinalIgnoreCase)
                || name.Contains("System.Composition", StringComparison.OrdinalIgnoreCase))
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();

        try
        {
            (HashSet<Asset>? inventory, string inventoryMessage) = await bot.Actions.GetInventory(
                Asset.SteamAppID,
                Asset.SteamCommunityContextID,
                language: "english").ConfigureAwait(false);

            return JsonSerializer.Serialize(new
            {
                success = inventory is not null,
                mode = "diagnostic-read-only",
                sendEnabled = false,
                tradeOfferCreated = false,
                botName = bot.BotName,
                botConnected = bot.IsConnectedAndLoggedOn,
                pluginAssembly = GetAssemblyDiagnostics(pluginAssembly),
                asfAssembly = GetAssemblyDiagnostics(asfAssembly),
                steamKitAssembly = GetAssemblyDiagnostics(steamKitAssembly),
                pluginReferences,
                actionsType = actions.GetType().FullName ?? actions.GetType().Name,
                actionsAssembly = GetAssemblyDiagnostics(actions.GetType().Assembly),
                getInventoryMethods = inventoryMethods,
                confirmationMethods,
                webHandlerType = webHandler.GetType().FullName ?? webHandler.GetType().Name,
                webHandlerAssembly = GetAssemblyDiagnostics(webHandler.GetType().Assembly),
                sendTradeOfferMethods,
                runtimeTrimming,
                inventoryCount = inventory?.Count,
                inventoryMessage = inventory is null ? "ASF не вернул инвентарь." : null,
                message = inventory is null
                    ? "Диагностика завершилась: ASF не вернул инвентарь. Отправка не выполнялась."
                    : "Диагностика успешна: инвентарь получен. Торговое предложение не создавалось."
            }, JsonOptions);
        }
        catch (Exception exception)
        {
            return JsonSerializer.Serialize(new
            {
                success = false,
                mode = "diagnostic-read-only",
                sendEnabled = false,
                tradeOfferCreated = false,
                botName = bot.BotName,
                botConnected = bot.IsConnectedAndLoggedOn,
                pluginAssembly = GetAssemblyDiagnostics(pluginAssembly),
                asfAssembly = GetAssemblyDiagnostics(asfAssembly),
                steamKitAssembly = GetAssemblyDiagnostics(steamKitAssembly),
                pluginReferences,
                actionsType = actions.GetType().FullName ?? actions.GetType().Name,
                actionsAssembly = GetAssemblyDiagnostics(actions.GetType().Assembly),
                getInventoryMethods = inventoryMethods,
                confirmationMethods,
                webHandlerType = webHandler.GetType().FullName ?? webHandler.GetType().Name,
                webHandlerAssembly = GetAssemblyDiagnostics(webHandler.GetType().Assembly),
                sendTradeOfferMethods,
                runtimeTrimming,
                exceptionType = exception.GetType().FullName,
                missingMemberName = FindMissingMemberException(exception) is MissingMemberException missingMember
                    ? GetMissingMemberField(missingMember, "MemberName") as string
                    : null,
                missingClassName = FindMissingMemberException(exception) is MissingMemberException missingClass
                    ? GetMissingMemberField(missingClass, "ClassName") as string
                    : null,
                missingSignature = FindMissingMemberException(exception) is MissingMemberException missingSignatureException
                    && GetMissingMemberField(missingSignatureException, "Signature") is byte[] signatureBytes
                    ? Convert.ToHexString(signatureBytes)
                    : null,
                message = "Ошибка диагностического чтения инвентаря. Отправка не выполнялась."
            }, JsonOptions);
        }
    }

    private static async Task<string> RunBadgeDiagnosticCommand(
        Bot bot,
        uint[] appIds,
        ulong? requestedRecipientSteamId64)
    {
        ReadOnlyDiagnostic.ProfileIdentityResult? recipientIdentity = null;
        if (requestedRecipientSteamId64 is ulong requestedSteamId64)
        {
            var profileResponse = await bot.ArchiWebHandler.UrlGetToHtmlDocumentWithSession(
                new Uri("https://steamcommunity.com/profiles/"
                    + requestedSteamId64.ToString(CultureInfo.InvariantCulture)
                    + "/?xml=1")).ConfigureAwait(false);

            string profileXml = profileResponse?.Content?.DocumentElement?.OuterHtml ?? "";
            System.Net.HttpStatusCode? profileStatusCode = profileResponse?.StatusCode;
            recipientIdentity = profileStatusCode == System.Net.HttpStatusCode.OK
                ? ReadOnlyDiagnostic.VerifyProfileIdentity(profileXml, requestedSteamId64)
                : ReadOnlyDiagnostic.ProfileIdentityResult.Failure("profile-http-error");

            if (recipientIdentity is not { Verified: true })
            {
                return JsonSerializer.Serialize(new
                {
                    success = false,
                    mode = "diagnostic-read-only",
                    sendEnabled = false,
                    tradeOfferCreated = false,
                    recipientSteamId64 = requestedSteamId64,
                    recipientIdentity = new
                    {
                        verified = false,
                        error = recipientIdentity?.Error ?? "profile-identity-unavailable",
                        httpStatusCode = profileStatusCode is System.Net.HttpStatusCode statusCode
                            ? (int)statusCode
                            : (int?)null
                    },
                    badgeChecks = Array.Empty<object>(),
                    inventory = (object?)null,
                    comparisons = Array.Empty<object>(),
                    message = "Профиль получателя не подтверждён; badge и инвентарь не запрашивались."
                }, JsonOptions);
            }
        }

        List<object> badgeChecks = [];
        List<(uint AppId, ReadOnlyDiagnostic.BadgeResult Badge)> recipientBadges = [];
        bool allBadgesValid = true;

        foreach (uint appId in appIds)
        {
            var myResponse = await bot.ArchiWebHandler.UrlGetToJsonObjectWithSession<JsonElement>(
                new Uri("https://steamcommunity.com/my/ajaxgetbadgeinfo/"
                    + appId.ToString(CultureInfo.InvariantCulture))).ConfigureAwait(false);
            System.Net.HttpStatusCode myStatusCode = myResponse?.StatusCode
                ?? System.Net.HttpStatusCode.ServiceUnavailable;
            JsonElement myContent = myResponse?.Content ?? default;
            ReadOnlyDiagnostic.BadgeResult myBadge = myStatusCode == System.Net.HttpStatusCode.OK
                && myContent.ValueKind == JsonValueKind.Object
                    ? ReadOnlyDiagnostic.ParseBadge(myContent.GetRawText(), appId)
                    : ReadOnlyDiagnostic.BadgeResult.Failure("badge-response-unavailable");
            AddBadgeCheck("my", appId, myStatusCode, myBadge);
            allBadgesValid &= myBadge.Success;

            if (requestedRecipientSteamId64 is ulong recipientId)
            {
                Uri recipientBadgeUri = new("https://steamcommunity.com/profiles/"
                    + recipientId.ToString(CultureInfo.InvariantCulture)
                    + "/ajaxgetbadgeinfo/"
                    + appId.ToString(CultureInfo.InvariantCulture));
                var recipientResponse = await bot.ArchiWebHandler.UrlGetToJsonObjectWithSession<JsonElement>(
                    recipientBadgeUri).ConfigureAwait(false);
                System.Net.HttpStatusCode recipientStatusCode = recipientResponse?.StatusCode
                    ?? System.Net.HttpStatusCode.ServiceUnavailable;
                JsonElement recipientContent = recipientResponse?.Content ?? default;
                bool recipientResponseIdentityVerified = recipientResponse?.FinalUri is Uri finalUri
                    && ReadOnlyDiagnostic.IsRecipientBadgeResponse(finalUri, recipientId, appId);
                ReadOnlyDiagnostic.BadgeResult recipientBadge = recipientStatusCode == System.Net.HttpStatusCode.OK
                    && recipientResponseIdentityVerified
                    && recipientContent.ValueKind == JsonValueKind.Object
                        ? ReadOnlyDiagnostic.ParseBadge(recipientContent.GetRawText(), appId)
                        : ReadOnlyDiagnostic.BadgeResult.Failure(recipientResponseIdentityVerified
                            ? "badge-response-unavailable"
                            : "recipient-badge-identity-mismatch");
                AddBadgeCheck(
                    "recipient",
                    appId,
                    recipientStatusCode,
                    recipientBadge,
                    recipientResponseIdentityVerified);
                recipientBadges.Add((appId, recipientBadge));
                allBadgesValid &= recipientBadge.Success;
            }
        }

        if (requestedRecipientSteamId64 is not ulong recipientSteamId64)
        {
            return JsonSerializer.Serialize(new
            {
                success = allBadgesValid,
                mode = "diagnostic-read-only",
                sendEnabled = false,
                tradeOfferCreated = false,
                botName = bot.BotName,
                recipientIdentity = (object?)null,
                badgeChecks,
                inventory = (object?)null,
                comparisons = Array.Empty<object>(),
                message = "Badge-диагностика ASF завершена; отправка не выполнялась."
            }, JsonOptions);
        }

        ReadOnlyDiagnostic.InventorySummary inventory = await ReadOnlyDiagnostic.AggregateInventoryAsync(
            ReadRecipientInventory()).ConfigureAwait(false);
        List<object> comparisons = [];
        bool allComparisonsValid = true;
        foreach ((uint appId, ReadOnlyDiagnostic.BadgeResult badge) in recipientBadges)
        {
            ReadOnlyDiagnostic.MatchResult match = ReadOnlyDiagnostic.CompareBadgeToInventory(
                badge,
                inventory,
                appId);
            comparisons.Add(new
            {
                appId,
                match.Success,
                match.Error,
                match.MatchedHashes,
                match.MissingHashes,
                match.ExtraHashes,
                match.DuplicateBadgeHashes,
                match.DuplicateInventoryHashes
            });
            allComparisonsValid &= match.Success;
        }

        return JsonSerializer.Serialize(new
        {
            success = allBadgesValid && allComparisonsValid,
            mode = "diagnostic-read-only",
            sendEnabled = false,
            tradeOfferCreated = false,
            botName = bot.BotName,
            recipientSteamId64,
            recipientIdentity = new { verified = true },
            badgeChecks,
            inventory,
            comparisons,
            message = "Badge и инвентарь получателя проверены через ASF; отправка не выполнялась."
        }, JsonOptions);

        void AddBadgeCheck(
            string subject,
            uint appId,
            System.Net.HttpStatusCode statusCode,
            ReadOnlyDiagnostic.BadgeResult badge,
            bool? responseIdentityVerified = null)
        {
            badgeChecks.Add(new
            {
                subject,
                appId,
                success = badge.Success,
                badge.Error,
                httpStatusCode = (int)statusCode,
                responseIdentityVerified,
                badge.EResult,
                badge.AppID,
                badge.Level,
                badge.MaxLevel,
                badge.Border,
                badge.CardCount,
                badge.UniqueHashCount,
                badge.DuplicateHashes
            });
        }

        async IAsyncEnumerable<ReadOnlyDiagnostic.InventoryEntry> ReadRecipientInventory()
        {
            await foreach (Asset asset in bot.ArchiWebHandler.GetInventoryAsync(
                recipientSteamId64,
                Asset.SteamAppID,
                Asset.SteamCommunityContextID).ConfigureAwait(false))
            {
                yield return new ReadOnlyDiagnostic.InventoryEntry(
                    asset.RealAppID,
                    asset.Description?.MarketHashName,
                    asset.Type.ToString(),
                    asset.Amount,
                    asset.Tradable);
            }
        }
    }

    private static bool TryParseDiagnosticArguments(
        string[] commandArgs,
        out uint[] appIds,
        out ulong? recipientSteamId64,
        out string error)
    {
        List<uint> parsedAppIds = [];
        HashSet<uint> uniqueAppIds = [];
        recipientSteamId64 = null;

        for (int index = 2; index < commandArgs.Length; index++)
        {
            string argument = commandArgs[index];
            if (string.Equals(argument, "--recipient", StringComparison.OrdinalIgnoreCase))
            {
                if (recipientSteamId64.HasValue
                    || ++index >= commandArgs.Length
                    || !ulong.TryParse(
                        commandArgs[index],
                        NumberStyles.None,
                        CultureInfo.InvariantCulture,
                        out ulong parsedSteamId64)
                    || parsedSteamId64 == 0
                    || !string.Equals(
                        commandArgs[index],
                        parsedSteamId64.ToString(CultureInfo.InvariantCulture),
                        StringComparison.Ordinal)
                    || !new SteamID(parsedSteamId64).IsIndividualAccount)
                {
                    error = "Получатель должен быть индивидуальным SteamID64 в десятичном формате.";
                    appIds = [];
                    return false;
                }

                recipientSteamId64 = parsedSteamId64;
                continue;
            }

            if (!uint.TryParse(
                    argument,
                    NumberStyles.None,
                    CultureInfo.InvariantCulture,
                    out uint appId)
                || appId == 0
                || !string.Equals(argument, appId.ToString(CultureInfo.InvariantCulture), StringComparison.Ordinal))
            {
                error = "AppID должен быть положительным каноническим десятичным числом.";
                appIds = [];
                return false;
            }

            if (!uniqueAppIds.Add(appId))
            {
                error = "Список AppID содержит повтор.";
                appIds = [];
                return false;
            }

            if (parsedAppIds.Count == MaxDiagnosticAppIds)
            {
                error = "За один запуск можно проверить не более 20 AppID.";
                appIds = [];
                return false;
            }

            parsedAppIds.Add(appId);
        }

        if (parsedAppIds.Count == 0)
        {
            error = "Для badge-проверки укажи хотя бы один AppID; без параметров доступна legacy-диагностика.";
            appIds = [];
            return false;
        }

        appIds = parsedAppIds.ToArray();
        error = "";
        return true;
    }

    private static object GetRuntimeTrimmingDiagnostics()
    {
        try
        {
            Type? sharedInfoType = typeof(Bot).Assembly.GetType(
                "ArchiSteamFarm.SharedInfo",
                throwOnError: false);
            if (sharedInfoType is null)
            {
                return new { status = "shared-info-type-not-found" };
            }

            PropertyInfo? property = sharedInfoType.GetProperty(
                "IsRuntimeTrimmed",
                BindingFlags.Public | BindingFlags.Static);
            if (property is null)
            {
                return new
                {
                    status = "runtime-trimmed-property-not-found",
                    sharedInfoType = sharedInfoType.FullName
                };
            }

            return new
            {
                status = "ok",
                sharedInfoType = sharedInfoType.FullName,
                isRuntimeTrimmed = property.GetValue(null)
            };
        }
        catch (Exception exception)
        {
            return new
            {
                status = "probe-error",
                exceptionType = exception.GetType().FullName,
                exceptionMessage = SafeMessage(exception.Message)
            };
        }
    }

    private static object GetAssemblyDiagnostics(System.Reflection.Assembly assembly)
    {
        string location;
        try
        {
            location = string.IsNullOrWhiteSpace(assembly.Location) ? "empty" : assembly.Location;
        }
        catch (Exception exception)
        {
            location = exception.GetType().Name + ": " + SafeMessage(exception.Message);
        }

        string moduleVersionId;
        try
        {
            moduleVersionId = assembly.ManifestModule.ModuleVersionId.ToString("D");
        }
        catch (Exception exception)
        {
            moduleVersionId = exception.GetType().Name + ": " + SafeMessage(exception.Message);
        }

        return new
        {
            fullName = assembly.FullName ?? "unknown",
            version = assembly.GetName().Version?.ToString() ?? "unknown",
            location,
            moduleVersionId
        };
    }

    private static string GetMethodSignatures(Type type, string methodName)
    {
        try
        {
            string[] methods = type.GetMethods(
                    System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.Static
                    | System.Reflection.BindingFlags.Public
                    | System.Reflection.BindingFlags.NonPublic)
                .Where(method => string.Equals(method.Name, methodName, StringComparison.Ordinal))
                .Select(method => method.ToString() ?? method.Name)
                .OrderBy(signature => signature, StringComparer.Ordinal)
                .ToArray();

            return methods.Length == 0 ? "NOT_FOUND" : string.Join(" | ", methods);
        }
        catch (Exception exception)
        {
            return "REFLECTION_ERROR: " + exception.GetType().Name + ": " + SafeMessage(exception.Message);
        }
    }

    private static string SafeDiagnosticText(string? message)
    {
        if (string.IsNullOrWhiteSpace(message))
        {
            return "unknown";
        }

        // Deliberately use only basic string/character operations here. This is
        // the last-resort error path: it must not throw and hide the real failure.
        const string tokenMarker = "token=";
        const string redactedMarker = "token=[REDACTED]";
        const int maxOutputLength = 6000;
        System.Text.StringBuilder output = new(Math.Min(message.Length, maxOutputLength));
        int position = 0;

        while (position < message.Length && output.Length < maxOutputLength)
        {
            bool isTokenMarker = position + tokenMarker.Length <= message.Length;
            for (int offset = 0; isTokenMarker && offset < tokenMarker.Length; offset++)
            {
                if (char.ToUpperInvariant(message[position + offset]) != char.ToUpperInvariant(tokenMarker[offset]))
                {
                    isTokenMarker = false;
                }
            }

            if (isTokenMarker)
            {
                output.Append(redactedMarker);
                position += tokenMarker.Length;
                while (position < message.Length
                    && !char.IsWhiteSpace(message[position])
                    && message[position] != '&'
                    && message[position] != '#')
                {
                    position++;
                }

                continue;
            }

            char current = message[position++];
            if (!char.IsControl(current))
            {
                output.Append(current);
            }
        }

        return output.ToString();
    }
    private static string SafeMessage(string? message)
    {
        if (string.IsNullOrWhiteSpace(message))
        {
            return "неизвестная ошибка";
        }

        return new string(message.Where(character => !char.IsControl(character)).Take(240).ToArray());
    }

    private static string DiagnosticFailure(string message) => JsonSerializer.Serialize(new
    {
        success = false,
        mode = "diagnostic-read-only",
        sendEnabled = false,
        tradeOfferCreated = false,
        error = message
    }, JsonOptions);

    private static string Failure(string message) => JsonSerializer.Serialize(new
    {
        success = false,
        error = message,
        sendEnabled = false
    }, JsonOptions);

    private sealed class PreviewRequest
    {
        public PreviewRequest() { }

        public string OrderId { get; set; } = "";
        public string BotName { get; set; } = "";
        public ulong TargetSteamId64 { get; set; }
        public uint AppId { get; set; } = Asset.SteamAppID;
        public ulong ContextId { get; set; } = Asset.SteamCommunityContextID;
        public List<string> AssetIds { get; set; } = [];
    }

    private sealed class PreviewResponse
    {
        public bool Success { get; set; }
        public string Mode { get; set; } = "preview-only";
        public bool SendEnabled { get; set; }
        public string OrderId { get; set; } = "";
        public string BotName { get; set; } = "";
        public ulong TargetSteamId64 { get; set; }
        public uint AppId { get; set; } = Asset.SteamAppID;
        public ulong ContextId { get; set; } = Asset.SteamCommunityContextID;
        public int AssetCount { get; set; }
        public List<PreviewAsset> Assets { get; set; } = [];
        public string Message { get; set; } = "";
    }

    private sealed class PreviewAsset
    {
        public string AssetId { get; set; } = "";
        public string Name { get; set; } = "";
        public string ClassId { get; set; } = "";
        public string InstanceId { get; set; } = "";
        public bool Tradable { get; set; }
    }
}
