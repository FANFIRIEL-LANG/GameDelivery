using System;
using System.Collections.Generic;
using System.Composition;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;
using System.Threading.Tasks;
using ArchiSteamFarm.Plugins.Interfaces;
using ArchiSteamFarm.Steam;
using ArchiSteamFarm.Steam.Data;
using ArchiSteamFarm.Steam.Storage;
using ArchiSteamFarm.Storage;
using SteamKit2;

namespace GameDelivery.ASFPlugin;

[Export(typeof(IPlugin))]
public sealed class GameDeliveryPlugin : IBotCommand2
{
    private const string PreviewCommandName = "gamedelivery-preview";
    private const string SendCommandName = "gamedelivery-send";
    private const string DiagnosticCommandName = "gamedelivery-diagnose";
    private const int MaxItemsPerPreview = 255;
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = true,
        TypeInfoResolver = new DefaultJsonTypeInfoResolver()
    };

    public string Name => nameof(GameDeliveryPlugin);

    public Version Version => typeof(GameDeliveryPlugin).Assembly.GetName().Version
        ?? new Version(1, 0, 0, 0);

    public Task OnLoaded() => Task.CompletedTask;

    public async Task<string?> OnBotCommand(
        Bot bot,
        EAccess access,
        string message,
        string[] args,
        ulong steamID = 0)
    {
        if (args.Length == 0)
        {
            return null;
        }

        bool isPreview = string.Equals(args[0], PreviewCommandName, StringComparison.OrdinalIgnoreCase);
        bool isSend = string.Equals(args[0], SendCommandName, StringComparison.OrdinalIgnoreCase);
        bool isDiagnostic = string.Equals(args[0], DiagnosticCommandName, StringComparison.OrdinalIgnoreCase);
        if (!isPreview && !isSend && !isDiagnostic)
        {
            return null;
        }

        if (access != EAccess.Owner)
        {
            return Failure("Команда доступна только ASF Owner.");
        }

        if (isDiagnostic)
        {
            if (args.Length != 2 || string.IsNullOrWhiteSpace(args[1]))
            {
                return Failure("Укажи имя бота: gamedelivery-diagnose <BotName>.");
            }

            Bot? diagnosticBot = Bot.BotsReadOnly?.Values.FirstOrDefault(candidate =>
                string.Equals(candidate.BotName, args[1], StringComparison.OrdinalIgnoreCase));
            if (diagnosticBot is null)
            {
                return Failure("Бот " + SafeMessage(args[1]) + " не найден в текущем экземпляре ASF.");
            }

            return await RunDiagnosticCommand(diagnosticBot).ConfigureAwait(false);
        }

        if (args.Length != 2 || args[1].Length > 16000)
        {
            return Failure("Некорректный запрос GameDelivery.");
        }

        if (isSend)
        {
            try
            {
                return await TradeDelivery.ProcessSendCommand(args[1]).ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                // The failure may have happened after Steam accepted the request.
                // Fail closed: surface the exact exception type/message and prevent automatic retries.
                return JsonSerializer.Serialize(new
                {
                    success = false,
                    mode = "send",
                    sendEnabled = true,
                    resultUnknown = true,
                    error = "ASF прервал обработку отправки: "
                        + exception.GetType().Name + ": " + SafeMessage(exception.Message)
                        + ". Проверь исходящие предложения Steam; повторная отправка заблокирована."
                }, JsonOptions);
            }
        }

        PreviewRequest? request;
        try
        {
            string encoded = args[1].Replace('-', '+').Replace('_', '/');
            encoded = encoded.PadRight(encoded.Length + ((4 - encoded.Length % 4) % 4), '=');
            byte[] bytes = Convert.FromBase64String(encoded);
            request = JsonSerializer.Deserialize<PreviewRequest>(bytes, JsonOptions);
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
                !ulong.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out ulong id) || id == 0))
        {
            return Failure("В списке есть некорректный asset ID.");
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

            if (!string.Equals(asset.Type.ToString(), "TradingCard", StringComparison.Ordinal))
            {
                return Failure("Актив " + rawAssetId + " не является обычной Steam-карточкой.");
            }

            previewAssets.Add(new PreviewAsset
            {
                AssetId = asset.AssetID.ToString(CultureInfo.InvariantCulture),
                Name = asset.Description?.Name ?? "Steam Trading Card",
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

    private static async Task<string> RunDiagnosticCommand(Bot bot)
    {
        System.Reflection.Assembly asfAssembly = typeof(Bot).Assembly;
        string asfVersion = asfAssembly.GetName().Version?.ToString() ?? "unknown";
        string asfAssemblyLocation = SafeMessage(asfAssembly.Location);
        object actions = bot.Actions;
        object webHandler = bot.ArchiWebHandler;
        string actionsType = actions.GetType().FullName ?? actions.GetType().Name;
        string webHandlerType = webHandler.GetType().FullName ?? webHandler.GetType().Name;
        string inventoryMethods = string.Join(" | ", actions.GetType()
            .GetMethods()
            .Where(method => string.Equals(method.Name, "GetInventory", StringComparison.Ordinal))
            .Select(method => method.ToString()));
        string sendTradeOfferMethods = string.Join(" | ", webHandler.GetType()
            .GetMethods()
            .Where(method => string.Equals(method.Name, "SendTradeOffer", StringComparison.Ordinal))
            .Select(method => method.ToString()));

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
                asfAssemblyVersion = asfVersion,
                asfAssemblyLocation,
                actionsType,
                getInventoryMethods = inventoryMethods,
                webHandlerType,
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
                asfAssemblyVersion = asfVersion,
                asfAssemblyLocation,
                actionsType,
                getInventoryMethods = inventoryMethods,
                webHandlerType,
                sendTradeOfferMethods,
                exceptionType = exception.GetType().FullName,
                exceptionMessage = SafeMessage(exception.Message),
                message = "Ошибка диагностического чтения инвентаря. Отправка не выполнялась."
            }, JsonOptions);
        }
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
