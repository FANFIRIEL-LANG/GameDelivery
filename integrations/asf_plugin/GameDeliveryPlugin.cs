using System;
using System.Collections;
using System.Collections.Generic;
using System.Composition;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using System.Text;
using System.Threading.Tasks;
using System.Reflection;
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
    private const int MaxItemsPerPreview = 255;
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = true
    };

    [JsonInclude]
    public string Name => nameof(GameDeliveryPlugin);

    [JsonInclude]
    public Version Version => typeof(GameDeliveryPlugin).Assembly.GetName().Version
        ?? new Version(1, 0, 2, 0);

    public Task OnLoaded() => Task.CompletedTask;

    public Task OnASFInit(IReadOnlyDictionary<string, JsonElement>? additionalConfigProperties = null)
    {
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

    public async Task<string?> OnBotCommand(
        Bot bot,
        EAccess access,
        string message,
        string[] args,
        ulong steamID = 0)
    {
        bool isSend = false;
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
            if (!isPreview && !isSend && !isDiagnostic)
            {
                return null;
            }

            ASF.ArchiLogger.LogGenericInfo(
                "GameDeliveryPlugin.OnBotCommand: command=" + recognizedCommand
                + "; argumentCount=" + (commandArgs.Length - 1).ToString(CultureInfo.InvariantCulture)
                + "; bot=" + SafeMessage(bot.BotName) + ".");

            if (access != EAccess.Owner)
            {
                return Failure("Команда доступна только ASF Owner.");
            }

            if (isDiagnostic)
            {
                if (commandArgs.Length != 2 || string.IsNullOrWhiteSpace(commandArgs[1]))
                {
                    return Failure("Укажи имя бота: gamedelivery-diagnose <BotName>.");
                }

                Bot? diagnosticBot = Bot.BotsReadOnly?.Values.FirstOrDefault(candidate =>
                    string.Equals(candidate.BotName, commandArgs[1], StringComparison.OrdinalIgnoreCase));
                if (diagnosticBot is null)
                {
                    return Failure("Бот " + SafeMessage(commandArgs[1]) + " не найден в текущем экземпляре ASF.");
                }

                return await RunDiagnosticCommand(diagnosticBot).ConfigureAwait(false);
            }

            if (commandArgs.Length != 2 || commandArgs[1].Length > 16000)
            {
                return Failure("Некорректный запрос GameDelivery.");
            }

            if (isSend)
            {
                return await TradeDelivery.ProcessSendCommand(commandArgs[1]).ConfigureAwait(false);
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
                Asset.SteamAppID,
                Asset.SteamCommunityContextID,
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

                if (asset.AppID != Asset.SteamAppID || asset.ContextID != Asset.SteamCommunityContextID)
                {
                    return Failure("Актив " + rawAssetId + " относится к неподдерживаемому контексту.");
                }

                if (!asset.Tradable)
                {
                    return Failure("Актив " + rawAssetId + " сейчас нельзя передать.");
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
                AssetCount = previewAssets.Count,
                Assets = previewAssets,
                Message = "ASF подтвердил состав черновика. Торговое предложение не создавалось и не отправлялось."
            }, JsonOptions);
        }
        catch (Exception exception)
        {
            string safeMessage = SafeDiagnosticText(exception.Message);
            string safeDetails = SafeDiagnosticText(exception.ToString());
            ASF.ArchiLogger.LogGenericError(
                "GameDeliveryPlugin.OnBotCommand failed: command="
                + (recognizedCommand ?? "unresolved")
                + "; exception=" + exception.GetType().Name
                + "; details=" + safeDetails);

            return JsonSerializer.Serialize(new
            {
                success = false,
                mode = isSend ? "send" : "plugin-command",
                sendEnabled = isSend,
                resultUnknown = isSend,
                error = isSend
                    ? "ASF прервал обработку отправки. Результат требует ручной сверки; автоматический повтор запрещён."
                    : "Ошибка обработчика GameDelivery ASF: " + exception.GetType().Name + ": " + safeMessage,
                exceptionType = exception.GetType().FullName,
                exceptionMessage = safeMessage,
                exceptionDetails = safeDetails
            }, JsonOptions);
        }
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
                inventoryCount = inventory?.Count,
                inventoryMessage = inventory is null ? SafeMessage(inventoryMessage) : null,
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
                exceptionType = exception.GetType().FullName,
                exceptionMessage = SafeMessage(exception.Message),
                message = "Ошибка диагностического чтения инвентаря. Отправка не выполнялась."
            }, JsonOptions);
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
