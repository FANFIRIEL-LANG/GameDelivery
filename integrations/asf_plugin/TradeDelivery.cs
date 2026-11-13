using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using ArchiSteamFarm.Steam;
using ArchiSteamFarm.Steam.Data;
using ArchiSteamFarm.Steam.Security;
using SteamKit2;

namespace GameDelivery.ASFPlugin;

internal static class TradeDelivery
{
    private const int MaxItems = 255;
    private const uint Cs2AppId = 730;
    private const ulong Cs2ContextId = 2;
    private static readonly SemaphoreSlim SendSemaphore = new(1, 1);
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = true,
        TypeInfoResolver = new DefaultJsonTypeInfoResolver()
    };

    [DllImport("advapi32.dll", EntryPoint = "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ConvertStringSecurityDescriptorToSecurityDescriptor(
        string securityDescriptor, uint revision, out IntPtr descriptor, out uint descriptorSize);

    [DllImport("advapi32.dll", EntryPoint = "GetSecurityDescriptorDacl", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetSecurityDescriptorDacl(
        IntPtr descriptor,
        [MarshalAs(UnmanagedType.Bool)] out bool daclPresent,
        out IntPtr dacl,
        [MarshalAs(UnmanagedType.Bool)] out bool daclDefaulted);

    [DllImport("advapi32.dll", EntryPoint = "SetNamedSecurityInfoW",
        CharSet = CharSet.Unicode, ExactSpelling = true)]
    private static extern uint SetNamedSecurityInfo(
        string objectName, uint objectType, uint securityInfo,
        IntPtr owner, IntPtr group, IntPtr dacl, IntPtr sacl);

    [DllImport("kernel32.dll", EntryPoint = "LocalFree", SetLastError = true)]
    private static extern IntPtr LocalFree(IntPtr memory);

    private static void RestrictStoragePermissions(string path, bool isDirectory)
    {
        try
        {
            if (OperatingSystem.IsWindows())
            {
                string sddl = isDirectory
                    ? "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"
                    : "D:P(A;;FA;;;OW)(A;;FA;;;SY)";
                if (!ConvertStringSecurityDescriptorToSecurityDescriptor(
                    sddl, 1, out IntPtr descriptor, out _))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }

                try
                {
                    if (!GetSecurityDescriptorDacl(
                        descriptor, out bool daclPresent, out IntPtr dacl, out _)
                        || !daclPresent || dacl == IntPtr.Zero)
                    {
                        throw new Win32Exception(Marshal.GetLastWin32Error());
                    }

                    // SE_FILE_OBJECT; DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION.
                    uint result = SetNamedSecurityInfo(
                        path, 1, 0x80000004, IntPtr.Zero, IntPtr.Zero, dacl, IntPtr.Zero);
                    if (result != 0)
                    {
                        throw new Win32Exception((int) result);
                    }
                }
                finally
                {
                    LocalFree(descriptor);
                }

                return;
            }

            if (OperatingSystem.IsLinux() || OperatingSystem.IsMacOS())
            {
                File.SetUnixFileMode(
                    path,
                    isDirectory
                        ? UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute
                        : UnixFileMode.UserRead | UnixFileMode.UserWrite);
                return;
            }

            throw new PlatformNotSupportedException("Unsupported ACL platform for ASF plugin storage");
        }
        catch (Exception exception) when (
            exception is Win32Exception or IOException or UnauthorizedAccessException
                or PlatformNotSupportedException or DllNotFoundException or EntryPointNotFoundException)
        {
            throw new IOException(
                "Не удалось ограничить доступ к защищённому журналу отправок ASF-плагина.",
                exception);
        }
    }

    internal static bool IsSameSteamAccount(ulong botSteamId64, ulong recipientSteamId64)
        => botSteamId64 == recipientSteamId64;

    internal static bool IsSupportedInventoryContext(uint appId, ulong contextId)
        => (appId == Asset.SteamAppID && contextId == Asset.SteamCommunityContextID)
            || (appId == Cs2AppId && contextId == Cs2ContextId);

    internal static bool IsAssetEligible(
        uint assetAppId,
        ulong assetContextId,
        bool tradable,
        bool hasDescription,
        bool isNormalTradingCard,
        uint requestedAppId,
        ulong requestedContextId)
    {
        if (!IsSupportedInventoryContext(requestedAppId, requestedContextId)
            || assetAppId != requestedAppId
            || assetContextId != requestedContextId
            || !tradable
            || !hasDescription)
        {
            return false;
        }

        return requestedAppId == Cs2AppId && requestedContextId == Cs2ContextId
            || isNormalTradingCard;
    }

    internal static string GetTradeOfferStatusName(ETradeOfferState state) => state.ToString();

    internal static bool IsNormalTradingCard(Asset asset)
    {
        string itemType = asset.Description?.Type.ToString() ?? "";
        return string.Equals(itemType, "TradingCard", StringComparison.OrdinalIgnoreCase);
    }


    public static async Task<string> ProcessTradeStatusCommand(string botName, string rawTradeOfferId)
    {
        if (string.IsNullOrWhiteSpace(botName)
            || botName.Length > 128
            || string.IsNullOrWhiteSpace(rawTradeOfferId)
            || !ulong.TryParse(rawTradeOfferId, NumberStyles.None, CultureInfo.InvariantCulture, out ulong tradeOfferId)
            || tradeOfferId == 0
            || !string.Equals(rawTradeOfferId, tradeOfferId.ToString(CultureInfo.InvariantCulture), StringComparison.Ordinal))
        {
            return TradeStatusFailure("Укажи имя бота и канонический положительный TradeOfferID.");
        }

        Bot? targetBot = Bot.BotsReadOnly?.Values.FirstOrDefault(candidate =>
            string.Equals(candidate.BotName, botName, StringComparison.OrdinalIgnoreCase));
        if (targetBot is null)
        {
            return TradeStatusFailure("Выбранный ASF-бот не найден.");
        }

        if (!targetBot.IsConnectedAndLoggedOn)
        {
            return TradeStatusFailure("Выбранный ASF-бот не подключён к Steam; статус предложения не проверен.");
        }

        try
        {
            HashSet<TradeOffer>? sentOffers = await targetBot.ArchiWebHandler.GetTradeOffers(
                activeOffers: null,
                receivedOffers: false,
                sentOffers: true,
                withDescriptions: true).ConfigureAwait(false);

            if (sentOffers is null)
            {
                return TradeStatusFailure("ASF не смог получить список исходящих предложений. Состояние остаётся неизвестным.");
            }

            TradeOffer[] matches = sentOffers
                .Where(offer => offer.TradeOfferID == tradeOfferId)
                .ToArray();

            if (matches.Length == 0)
            {
                return JsonSerializer.Serialize(new
                {
                    success = false,
                    mode = "trade-status",
                    found = false,
                    tradeOfferId = rawTradeOfferId,
                    botName = targetBot.BotName,
                    status = "not_found_in_asf_response",
                    error = "ASF не вернул это исходящее предложение. Это не доказывает, что предложение отменено или отклонено."
                }, JsonOptions);
            }

            if (matches.Length != 1)
            {
                return TradeStatusFailure("ASF вернул неоднозначный результат для указанного TradeOfferID.");
            }

            TradeOffer offer = matches[0];
            return JsonSerializer.Serialize(new
            {
                success = true,
                mode = "trade-status",
                found = true,
                tradeOfferId = offer.TradeOfferID.ToString(CultureInfo.InvariantCulture),
                botName = targetBot.BotName,
                recipientSteamId64 = offer.OtherSteamID64.ToString(CultureInfo.InvariantCulture),
                status = GetTradeOfferStatusName(offer.State),
                itemsToGive = offer.ItemsToGiveReadOnly.Select(asset => new
                {
                    appId = asset.AppID,
                    contextId = asset.ContextID,
                    assetId = asset.AssetID.ToString(CultureInfo.InvariantCulture),
                    amount = asset.Amount,
                    tradable = asset.Tradable,
                    name = asset.Description?.Name
                }).ToArray()
            }, JsonOptions);
        }
        catch (Exception exception)
        {
            return TradeStatusFailure(
                "Не удалось проверить исходящее предложение через ASF: "
                + exception.GetType().Name + ": " + SafeMessage(exception.Message));
        }
    }

    internal static string TradeStatusFailure(string message) => JsonSerializer.Serialize(new
    {
        success = false,
        mode = "trade-status",
        found = false,
        status = "unknown",
        error = message
    }, JsonOptions);

    private static async Task<byte?> GetRecipientTradeHoldDurationAsync(
        Bot senderBot,
        ulong recipientSteamId64,
        string recipientTradeToken)
    {
        // ASF 6.3.11.7 does not expose its pre-offer combined-hold helper publicly.
        // Use the connected Bot's public OAuth token and ASF-configured HttpClientFactory
        // to call the same IEconService endpoint. No Steam Web API key is used.
        string? accessToken = senderBot.AccessToken;
        if (string.IsNullOrWhiteSpace(accessToken))
        {
            return null;
        }

        Uri endpoint = new(
            senderBot.SteamConfiguration.WebAPIBaseAddress,
            "IEconService/GetTradeHoldDurations/v1/");
        string query = "access_token=" + Uri.EscapeDataString(accessToken)
            + "&steamid_target=" + recipientSteamId64.ToString(CultureInfo.InvariantCulture)
            + "&trade_offer_access_token=" + Uri.EscapeDataString(recipientTradeToken);
        Uri requestUri = new UriBuilder(endpoint) { Query = query }.Uri;

        using HttpClient client = senderBot.SteamConfiguration.HttpClientFactory();
        using CancellationTokenSource timeout = new(TimeSpan.FromSeconds(15));
        using HttpRequestMessage request = new(HttpMethod.Get, requestUri);
        using HttpResponseMessage response = await client.SendAsync(
            request,
            HttpCompletionOption.ResponseHeadersRead,
            timeout.Token).ConfigureAwait(false);

        if (!response.IsSuccessStatusCode)
        {
            return null;
        }

        await using Stream responseStream = await response.Content.ReadAsStreamAsync(timeout.Token).ConfigureAwait(false);
        using JsonDocument document = await JsonDocument.ParseAsync(
            responseStream,
            cancellationToken: timeout.Token).ConfigureAwait(false);

        if (!document.RootElement.TryGetProperty("response", out JsonElement responseData)
            || !responseData.TryGetProperty("both_escrow", out JsonElement bothEscrow)
            || !bothEscrow.TryGetProperty("escrow_end_duration_seconds", out JsonElement secondsElement))
        {
            return null;
        }

        uint seconds;
        if (secondsElement.ValueKind == JsonValueKind.Number && secondsElement.TryGetUInt32(out uint numericSeconds))
        {
            seconds = numericSeconds;
        }
        else if (secondsElement.ValueKind == JsonValueKind.String
            && uint.TryParse(secondsElement.GetString(), NumberStyles.None, CultureInfo.InvariantCulture, out uint stringSeconds))
        {
            seconds = stringSeconds;
        }
        else
        {
            return null;
        }

        uint days = seconds == 0 ? 0 : seconds / 86400;
        return (byte)Math.Min(days, byte.MaxValue);
    }

    private static async Task<(bool InventoryAccessible, byte? TradeHoldDays, string? Error)> PreflightRecipientAsync(
        Bot senderBot,
        ulong recipientSteamId64,
        uint appId,
        ulong contextId,
        string recipientTradeToken)
    {
        try
        {
            await using IAsyncEnumerator<Asset> enumerator = senderBot.ArchiWebHandler.GetInventoryAsync(
                recipientSteamId64,
                appId,
                contextId,
                language: "english").GetAsyncEnumerator();

            // A successful first page (including a successfully returned empty inventory)
            // confirms that this Steam inventory is readable by the sender's ASF session.
            _ = await enumerator.MoveNextAsync().ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            return (
                false,
                null,
                "Инвентарь получателя недоступен через ASF в выбранном игровом контексте "
                + appId.ToString(CultureInfo.InvariantCulture) + "/" + contextId.ToString(CultureInfo.InvariantCulture)
                + ". Проверь приватность профиля/игры/инвентаря. "
                + exception.GetType().Name + ": " + SafeMessage(exception.Message, recipientTradeToken));
        }

        byte? tradeHoldDays;
        try
        {
            // ASF 6.3.11.7 exposes this read-only hold check internally. It uses ASF's
            // authenticated session/access token and recipient trade URL token, not a Steam Web API key.
            tradeHoldDays = await GetRecipientTradeHoldDurationAsync(
                senderBot,
                recipientSteamId64,
                recipientTradeToken).ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            // Do not echo request URLs or OAuth/trade tokens in logs or IPC responses.
            return (
                true,
                null,
                "ASF не смог достоверно проверить trade hold получателя; отправка заблокирована до успешной проверки ("
                + exception.GetType().Name + ").");
        }

        if (!tradeHoldDays.HasValue)
        {
            return (
                true,
                null,
                "ASF не вернул достоверный результат проверки trade hold получателя. Отправка заблокирована.");
        }

        if (tradeHoldDays.Value > 0)
        {
            return (
                true,
                tradeHoldDays,
                "У получателя обнаружено ограничение/удержание обмена минимум на "
                + tradeHoldDays.Value.ToString(CultureInfo.InvariantCulture)
                + " дн. Предложение не создавалось.");
        }

        return (true, tradeHoldDays, null);
    }

    public static async Task<string> ProcessSendCommand(
        string encodedPayload,
        string ipcContextBotName,
        Action onExternalDispatchStarted)
    {
        ArgumentNullException.ThrowIfNull(ipcContextBotName);
        ArgumentNullException.ThrowIfNull(onExternalDispatchStarted);
        SendRequest? request;
        try
        {
            string encoded = encodedPayload.Replace('-', '+').Replace('_', '/');
            encoded = encoded.PadRight(encoded.Length + ((4 - encoded.Length % 4) % 4), '=');
            request = JsonSerializer.Deserialize<SendRequest>(
                Encoding.UTF8.GetString(Convert.FromBase64String(encoded)),
                JsonOptions);
        }
        catch (Exception exception) when (exception is FormatException or JsonException)
        {
            return Failure("Не удалось разобрать запрос отправки GameDelivery.");
        }

        if (request is null
            || string.IsNullOrWhiteSpace(request.OrderId)
            || request.OrderId.Length > 128
            || request.OrderId.Any(char.IsControl)
            || string.IsNullOrWhiteSpace(request.BotName)
            || request.TargetSteamId64 == 0
            || request.AssetIds is null
            || request.AssetIds.Count is < 1 or > MaxItems
            || !IsSupportedInventoryContext(request.AppId, request.ContextId)
            || !Regex.IsMatch(request.CredentialNonce ?? "", "^[a-f0-9]{64}$", RegexOptions.CultureInvariant))
        {
            return Failure("В запросе отправки отсутствуют обязательные поля или они некорректны.");
        }

        if (request.AssetIds.Any(string.IsNullOrWhiteSpace)
            || request.AssetIds.Distinct(StringComparer.Ordinal).Count() != request.AssetIds.Count
            || request.AssetIds.Any(value =>
                !ulong.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out ulong id)
                || id == 0
                || !string.Equals(value, id.ToString(CultureInfo.InvariantCulture), StringComparison.Ordinal)))
        {
            return Failure("Список asset ID пустой, содержит повторы, неканонические или некорректные значения.");
        }

        PendingTradeSecret? secret;
        string secretPath = GetPendingSecretPath(request.CredentialNonce!);
        try
        {
            RestrictStoragePermissions(secretPath, isDirectory: false);
            string json = await File.ReadAllTextAsync(secretPath).ConfigureAwait(false);
            secret = JsonSerializer.Deserialize<PendingTradeSecret>(json, JsonOptions);
        }
        catch (Exception exception) when (
            exception is IOException or UnauthorizedAccessException or JsonException)
        {
            return Failure("Одноразовый секрет отправки отсутствует или не читается. Предложение не отправлено.");
        }

        try
        {
            File.Delete(secretPath);
            if (File.Exists(secretPath))
            {
                throw new IOException("The one-use trade secret still exists after deletion");
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            return Failure(
                "Не удалось удалить одноразовый секрет отправки. Предложение заблокировано; "
                + "проверь доступ к локальному файлу.");
        }

        if (secret is null
            || !string.Equals(secret.CredentialNonce, request.CredentialNonce, StringComparison.Ordinal)
            || !string.Equals(secret.OrderId, request.OrderId, StringComparison.Ordinal)
            || !string.Equals(secret.BotName, request.BotName, StringComparison.OrdinalIgnoreCase)
            || secret.TargetSteamId64 != request.TargetSteamId64
            || secret.AppId != request.AppId
            || secret.ContextId != request.ContextId
            || secret.AssetIds is null
            || !secret.AssetIds.SequenceEqual(request.AssetIds, StringComparer.Ordinal)
            || string.IsNullOrWhiteSpace(secret.TradeToken)
            || !Regex.IsMatch(secret.TradeToken, "^[A-Za-z0-9_-]{1,128}$", RegexOptions.CultureInvariant))
        {
            return Failure("Одноразовый секрет не совпадает с запросом или содержит некорректный trade token.");
        }

        SteamID recipient = new(request.TargetSteamId64);
        if (!recipient.IsIndividualAccount)
        {
            return Failure("Получатель не является индивидуальным Steam-аккаунтом.");
        }

        string fingerprint = ComputeFingerprint(request);
        await SendSemaphore.WaitAsync().ConfigureAwait(false);
        try
        {
            Dictionary<string, SendRecord> records;
            try
            {
                records = LoadSendRecords();
            }
            catch (Exception exception) when (
                exception is IOException or UnauthorizedAccessException or JsonException)
            {
                return Failure("Нельзя прочитать журнал защиты от повторной отправки. Отправка заблокирована.");
            }

            if (records.TryGetValue(request.OrderId, out SendRecord? existing))
            {
                bool fingerprintMatches = string.Equals(existing.Fingerprint, fingerprint, StringComparison.Ordinal)
                    || (request.AppId == Asset.SteamAppID
                        && request.ContextId == Asset.SteamCommunityContextID
                        && string.Equals(existing.Fingerprint, ComputeLegacyFingerprint(request), StringComparison.Ordinal));
                if (!fingerprintMatches || existing.AppId != request.AppId || existing.ContextId != request.ContextId)
                {
                    return Failure(
                        "ID заказа уже связан с другой отправкой. Проверь исходящие предложения Steam.",
                        resultUnknown: true);
                }

                if (existing.State is "sent" or "awaiting_confirmation")
                {
                    return SerializeRecord(existing, duplicatePrevented: true);
                }

                return Failure(
                    "Для этого заказа уже была попытка отправки с неопределённым результатом. "
                    + "Проверь исходящие предложения Steam; повторная отправка заблокирована.",
                    resultUnknown: true);
            }

            Bot? targetBot = Bot.BotsReadOnly?.Values.FirstOrDefault(candidate =>
                string.Equals(candidate.BotName, request.BotName, StringComparison.OrdinalIgnoreCase));
            if (targetBot is null)
            {
                return Failure("Выбранный бот не найден в текущем экземпляре ASF.");
            }

            targetBot.ArchiLogger.LogGenericInfo(
                "GameDelivery send: ipcContextBot=" + SafeMessage(ipcContextBotName)
                + "; requestedSenderBot=" + SafeMessage(request.BotName)
                + "; resolvedSenderBot=" + SafeMessage(targetBot.BotName)
                + "; recipientSteamId64=" + request.TargetSteamId64.ToString(CultureInfo.InvariantCulture)
                + "; orderId=" + SafeMessage(request.OrderId)
                + "; assetCount=" + request.AssetIds.Count.ToString(CultureInfo.InvariantCulture) + ".");

            if (!targetBot.IsConnectedAndLoggedOn)
            {
                return Failure("Выбранный бот ASF не подключён к Steam.");
            }

            if (IsSameSteamAccount(targetBot.SteamID, request.TargetSteamId64))
            {
                return Failure("Нельзя отправлять предложение самому себе.");
            }

            HashSet<Asset>? inventory;
            string inventoryMessage;
            DateTimeOffset inventoryStartedAt = DateTimeOffset.UtcNow;
            targetBot.ArchiLogger.LogGenericInfo(
                "GameDelivery send phase: phase=inventory_fetch_started; orderId="
                + SafeMessage(request.OrderId) + ".");
            try
            {
                (inventory, inventoryMessage) = await targetBot.Actions.GetInventory(
                    request.AppId,
                    request.ContextId,
                    language: "english").ConfigureAwait(false);
                targetBot.ArchiLogger.LogGenericInfo(
                    "GameDelivery send phase: phase=inventory_fetch_completed; orderId="
                    + SafeMessage(request.OrderId)
                    + "; elapsedMs=" + (DateTimeOffset.UtcNow - inventoryStartedAt).TotalMilliseconds.ToString("F0", CultureInfo.InvariantCulture)
                    + "; assetCount=" + (inventory?.Count ?? 0).ToString(CultureInfo.InvariantCulture) + ".");
            }
            catch (MissingMethodException exception)
            {
                return Failure(
                    "Проверка инвентаря ASF завершилась ошибкой до отправки: MissingMethodException: "
                    + SafeMessage(exception.Message)
                    + ". Предложение не отправлялось.",
                    resultUnknown: false);
            }
            catch (Exception exception)
            {
                return Failure(
                    "Проверка инвентаря ASF завершилась ошибкой до отправки: "
                    + exception.GetType().Name + ": " + SafeMessage(exception.Message)
                    + ". Предложение не отправлялось.",
                    resultUnknown: false);
            }

            if (inventory is null)
            {
                return Failure("Не удалось проверить актуальный инвентарь ASF: " + SafeMessage(inventoryMessage));
            }

            Dictionary<ulong, Asset> byAssetId = inventory
                .Where(asset => asset.AssetID > 0)
                .GroupBy(asset => asset.AssetID)
                .ToDictionary(group => group.Key, group => group.First());
            List<Asset> selectedAssets = new(request.AssetIds.Count);
            foreach (string rawAssetId in request.AssetIds)
            {
                ulong assetId = ulong.Parse(rawAssetId, NumberStyles.None, CultureInfo.InvariantCulture);
                if (!byAssetId.TryGetValue(assetId, out Asset? asset))
                {
                    return Failure("Актив " + rawAssetId + " отсутствует в актуальном инвентаре выбранного бота.");
                }

                if (!IsAssetEligible(
                    asset.AppID,
                    asset.ContextID,
                    asset.Tradable,
                    asset.Description is not null,
                    IsNormalTradingCard(asset),
                    request.AppId,
                    request.ContextId))
                {
                    return Failure(
                        "Актив " + rawAssetId + " не соответствует выбранной игре/контексту, "
                        + "не имеет подтверждённого описания, не передаваем или не является обычной Steam Trading Card.");
                }

                selectedAssets.Add(asset);
            }

            (bool recipientInventoryAccessible, byte? recipientTradeHoldDays, string? recipientPreflightError) =
                await PreflightRecipientAsync(
                    targetBot,
                    request.TargetSteamId64,
                    request.AppId,
                    request.ContextId,
                    secret.TradeToken).ConfigureAwait(false);
            targetBot.ArchiLogger.LogGenericInfo(
                "GameDelivery send phase: phase=recipient_preflight_completed; orderId="
                + SafeMessage(request.OrderId)
                + "; inventoryAccessible=" + recipientInventoryAccessible.ToString(CultureInfo.InvariantCulture)
                + "; reportedTradeHoldDays=" + (recipientTradeHoldDays?.ToString(CultureInfo.InvariantCulture) ?? "unknown") + ".");
            if (recipientPreflightError is not null)
            {
                return Failure(recipientPreflightError);
            }

            SendRecord record = new()
            {
                OrderId = request.OrderId,
                Fingerprint = fingerprint,
                BotName = targetBot.BotName,
                TargetSteamId64 = request.TargetSteamId64,
                AppId = request.AppId,
                ContextId = request.ContextId,
                AssetIds = request.AssetIds.ToList(),
                State = "sending",
                UpdatedAtUtc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture)
            };
            records.Add(request.OrderId, record);
            try
            {
                SaveSendRecords(records);
            }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
            {
                return Failure("Не удалось сохранить защиту от повторной отправки. Предложение не отправлено.");
            }

            try
            {
                // From this point onward the result may be unknown if ASF or Steam fails.
                onExternalDispatchStarted();
                DateTimeOffset sendStartedAt = DateTimeOffset.UtcNow;
                targetBot.ArchiLogger.LogGenericInfo(
                    "GameDelivery send phase: phase=steam_offer_dispatch_started; orderId="
                    + SafeMessage(request.OrderId)
                    + "; assetCount=" + selectedAssets.Count.ToString(CultureInfo.InvariantCulture) + ".");
                (bool success, HashSet<ulong>? tradeOfferIds, HashSet<ulong>? mobileTradeOfferIds) =
                    await targetBot.ArchiWebHandler.SendTradeOffer(
                        request.TargetSteamId64,
                        itemsToGive: selectedAssets,
                        token: secret.TradeToken,
                        // ASF always adds its own fixed "Sent by ..." identifier. Empty suppresses only our optional custom suffix.
                        customMessage: string.Empty,
                        forcedSingleOffer: true,
                        itemsPerTrade: (ushort)selectedAssets.Count).ConfigureAwait(false);
                targetBot.ArchiLogger.LogGenericInfo(
                    "GameDelivery send phase: phase=steam_offer_dispatch_completed; orderId="
                    + SafeMessage(request.OrderId)
                    + "; elapsedMs=" + (DateTimeOffset.UtcNow - sendStartedAt).TotalMilliseconds.ToString("F0", CultureInfo.InvariantCulture)
                    + "; success=" + success.ToString(CultureInfo.InvariantCulture)
                    + "; regularOfferCount=" + (tradeOfferIds?.Count ?? 0).ToString(CultureInfo.InvariantCulture)
                    + "; mobileOfferCount=" + (mobileTradeOfferIds?.Count ?? 0).ToString(CultureInfo.InvariantCulture) + ".");

                HashSet<ulong> allOfferIds = new();
                if (tradeOfferIds is not null) allOfferIds.UnionWith(tradeOfferIds);
                if (mobileTradeOfferIds is not null) allOfferIds.UnionWith(mobileTradeOfferIds);
                record.OfferIds = allOfferIds.Order()
                    .Select(id => id.ToString(CultureInfo.InvariantCulture)).ToList();

                // An ID alone is not enough to report success if ASF returned success=false.
                // Preserve any known IDs and lock the order for manual inspection.
                if (allOfferIds.Count == 0 || !success)
                {
                    string unknownMessage = allOfferIds.Count == 0
                        ? "ASF не вернул ID предложения. Результат неизвестен; проверь исходящие предложения Steam и не повторяй отправку."
                        : "ASF вернул ID предложения, но не подтвердил успешную отправку. Проверь исходящие предложения Steam и не повторяй отправку.";
                    record.State = "unknown";
                    record.Message = unknownMessage;
                    record.UpdatedAtUtc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
                    try
                    {
                        SaveSendRecords(records);
                    }
                    catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
                    {
                        record.Message = unknownMessage + " Локальный журнал тоже не удалось обновить.";
                    }

                    return UnknownFailure(record, record.Message ?? unknownMessage, tradeToken: secret.TradeToken);
                }

                bool confirmationPending = mobileTradeOfferIds?.Count > 0;
                if (confirmationPending && targetBot.HasMobileAuthenticator)
                {
                    DateTimeOffset confirmationStartedAt = DateTimeOffset.UtcNow;
                    targetBot.ArchiLogger.LogGenericInfo(
                        "GameDelivery send phase: phase=mobile_confirmation_started; orderId="
                        + SafeMessage(request.OrderId)
                        + "; mobileOfferCount=" + mobileTradeOfferIds!.Count.ToString(CultureInfo.InvariantCulture) + ".");
                    (bool twoFactorSuccess, _, string confirmationMessage) =
                        await targetBot.Actions.HandleTwoFactorAuthenticationConfirmations(
                            true,
                            EMobileConfirmationType.Trade,
                            mobileTradeOfferIds,
                            waitIfNeeded: false).ConfigureAwait(false);
                    confirmationPending = !twoFactorSuccess;
                    record.Message = SafeMessage(confirmationMessage, secret.TradeToken);
                    targetBot.ArchiLogger.LogGenericInfo(
                        "GameDelivery send phase: phase=mobile_confirmation_completed; orderId="
                        + SafeMessage(request.OrderId)
                        + "; elapsedMs=" + (DateTimeOffset.UtcNow - confirmationStartedAt).TotalMilliseconds.ToString("F0", CultureInfo.InvariantCulture)
                        + "; confirmed=" + twoFactorSuccess.ToString(CultureInfo.InvariantCulture) + ".");
                }

                record.State = confirmationPending ? "awaiting_confirmation" : "sent";
                record.UpdatedAtUtc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
                SaveSendRecords(records);
                return SerializeRecord(record, duplicatePrevented: false, sendSucceeded: success);
            }
            catch (Exception exception)
            {
                targetBot.ArchiLogger.LogGenericError(
                    "GameDelivery send phase: phase=send_pipeline_failed; orderId="
                    + SafeMessage(request.OrderId)
                    + "; exceptionType=" + exception.GetType().Name
                    + "; message=" + SafeMessage(exception.Message, secret.TradeToken) + ".");
                record.State = "unknown";
                record.Message = "Exception while sending; do not retry automatically";
                record.UpdatedAtUtc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
                try { SaveSendRecords(records); }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
                return UnknownFailure(
                    record,
                    "Не удалось однозначно подтвердить результат отправки. Проверь исходящие предложения Steam; повторная отправка заблокирована.",
                    exception,
                    secret.TradeToken);
            }
        }
        finally
        {
            SendSemaphore.Release();
        }
    }

    private static string GetPendingSecretPath(string nonce)
    {
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrWhiteSpace(localAppData))
        {
            localAppData = AppContext.BaseDirectory;
        }

        return Path.Combine(localAppData, "GameDelivery", "pending_trade_requests", nonce + ".json");
    }

    private static string ComputeFingerprint(SendRequest request)
    {
        string canonical = string.Join("\n",
            request.OrderId,
            request.BotName.Trim().ToLowerInvariant(),
            request.TargetSteamId64.ToString(CultureInfo.InvariantCulture),
            request.AppId.ToString(CultureInfo.InvariantCulture),
            request.ContextId.ToString(CultureInfo.InvariantCulture),
            string.Join(",", request.AssetIds.OrderBy(value => value, StringComparer.Ordinal)));
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)));
    }

    private static string ComputeLegacyFingerprint(SendRequest request)
    {
        string canonical = string.Join("\n",
            request.OrderId,
            request.BotName.Trim().ToLowerInvariant(),
            request.TargetSteamId64.ToString(CultureInfo.InvariantCulture),
            string.Join(",", request.AssetIds.OrderBy(value => value, StringComparer.Ordinal)));
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)));
    }

    private static Dictionary<string, SendRecord> LoadSendRecords()
    {
        string path = GetSendRecordPath();
        string directory = Path.GetDirectoryName(path) ?? throw new IOException("Missing data directory");
        if (Directory.Exists(directory))
        {
            RestrictStoragePermissions(directory, isDirectory: true);
        }

        if (!File.Exists(path))
        {
            return new Dictionary<string, SendRecord>(StringComparer.OrdinalIgnoreCase);
        }

        RestrictStoragePermissions(path, isDirectory: false);
        Dictionary<string, SendRecord>? loaded = JsonSerializer.Deserialize<Dictionary<string, SendRecord>>(
            File.ReadAllText(path), JsonOptions);
        if (loaded is null)
        {
            throw new JsonException("Invalid delivery send record");
        }

        return new Dictionary<string, SendRecord>(loaded, StringComparer.OrdinalIgnoreCase);
    }

    private static string GetSendRecordPath()
    {
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (string.IsNullOrWhiteSpace(localAppData))
        {
            localAppData = AppContext.BaseDirectory;
        }

        return Path.Combine(localAppData, "GameDelivery", "ASFPlugin", "sent_offers.json");
    }

    private static void SaveSendRecords(Dictionary<string, SendRecord> records)
    {
        string path = GetSendRecordPath();
        string directory = Path.GetDirectoryName(path) ?? throw new IOException("Missing data directory");
        Directory.CreateDirectory(directory);
        RestrictStoragePermissions(directory, isDirectory: true);
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            byte[] payload = new UTF8Encoding(false).GetBytes(JsonSerializer.Serialize(records, JsonOptions));
            using (FileStream stream = new(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                // Apply the protected ACL before writing account/offer metadata to disk.
                RestrictStoragePermissions(temporary, isDirectory: false);
                stream.Write(payload, 0, payload.Length);
                stream.Flush(flushToDisk: true);
            }

            File.Move(temporary, path, overwrite: true);
            RestrictStoragePermissions(path, isDirectory: false);
        }
        finally
        {
            try { File.Delete(temporary); } catch (IOException) { }
        }
    }

    private static string SerializeRecord(SendRecord record, bool duplicatePrevented, bool sendSucceeded = true)
    {
        return JsonSerializer.Serialize(new
        {
            success = true,
            mode = "sent",
            sendEnabled = true,
            duplicatePrevented,
            orderId = record.OrderId,
            botName = record.BotName,
            targetSteamId64 = record.TargetSteamId64,
            appId = record.AppId,
            contextId = record.ContextId,
            assetCount = record.AssetIds.Count,
            assets = record.AssetIds.Select(id => new { assetId = id }).ToList(),
            offerIds = record.OfferIds,
            confirmationStatus = record.State == "awaiting_confirmation" ? "pending" : "handled",
            message = record.Message ?? (sendSucceeded
                ? "Предложение создано; ожидание действий получателя ещё не завершено."
                : "Предложение создано, но ASF сообщил о предупреждении; проверь статус в Steam."),
            tradeAccepted = false
        }, JsonOptions);
    }

    private static string UnknownFailure(
        SendRecord record,
        string message,
        Exception? exception = null,
        string? tradeToken = null) => JsonSerializer.Serialize(new
    {
        success = false,
        mode = "send",
        sendEnabled = true,
        resultUnknown = true,
        error = message,
        exceptionType = exception?.GetType().FullName,
        exceptionMessage = exception is null ? null : SafeDiagnosticText(exception.Message, tradeToken),
        missingMethod = exception is MissingMethodException ? SafeDiagnosticText(exception.Message, tradeToken) : null,
        targetSite = exception?.TargetSite?.ToString() is string targetSite ? SafeDiagnosticText(targetSite, tradeToken) : null,
        stackTrace = exception?.StackTrace is string stackTrace ? SafeDiagnosticText(stackTrace, tradeToken) : null,
        orderId = record.OrderId,
        botName = record.BotName,
        targetSteamId64 = record.TargetSteamId64,
        appId = record.AppId,
        contextId = record.ContextId,
        assetCount = record.AssetIds.Count,
        assets = record.AssetIds.Select(id => new { assetId = id }).ToList(),
        offerIds = record.OfferIds
    }, JsonOptions);

    private static string Failure(string message, bool resultUnknown = false) => JsonSerializer.Serialize(new
    {
        success = false,
        mode = "send",
        sendEnabled = true,
        resultUnknown,
        error = message
    }, JsonOptions);

    private static string SafeMessage(string? message, string? tradeToken = null)
    {
        if (string.IsNullOrWhiteSpace(message))
        {
            return "не удалось завершить подтверждение в мобильном приложении";
        }

        string sanitized = SafeDiagnosticText(message, tradeToken);
        return new string(sanitized.Where(character => !char.IsControl(character)).Take(180).ToArray());
    }

    private static string SafeDiagnosticText(string? message, string? tradeToken = null)
    {
        if (string.IsNullOrWhiteSpace(message))
        {
            return "unknown";
        }

        string sanitized = new string(message.Where(character => !char.IsControl(character)).Take(2400).ToArray());
        if (!string.IsNullOrWhiteSpace(tradeToken))
        {
            sanitized = sanitized.Replace(tradeToken, "[REDACTED]", StringComparison.Ordinal);
        }

        return Regex.Replace(
            sanitized,
            @"(?i)(token\s*=\s*)[^&\s]+",
            "$1[REDACTED]",
            RegexOptions.CultureInvariant);
    }

    private sealed class SendRequest
    {
        public SendRequest() { }

        public string OrderId { get; set; } = "";
        public string BotName { get; set; } = "";
        public ulong TargetSteamId64 { get; set; }
        public uint AppId { get; set; } = Asset.SteamAppID;
        public ulong ContextId { get; set; } = Asset.SteamCommunityContextID;
        public List<string> AssetIds { get; set; } = [];
        public string CredentialNonce { get; set; } = "";
    }

    private sealed class PendingTradeSecret
    {
        public PendingTradeSecret() { }

        public string CredentialNonce { get; set; } = "";
        public string OrderId { get; set; } = "";
        public string BotName { get; set; } = "";
        public ulong TargetSteamId64 { get; set; }
        public uint AppId { get; set; } = Asset.SteamAppID;
        public ulong ContextId { get; set; } = Asset.SteamCommunityContextID;
        public List<string> AssetIds { get; set; } = [];
        public string TradeToken { get; set; } = "";
    }

    private sealed class SendRecord
    {
        public SendRecord() { }

        public string OrderId { get; set; } = "";
        public string Fingerprint { get; set; } = "";
        public string BotName { get; set; } = "";
        public ulong TargetSteamId64 { get; set; }
        public uint AppId { get; set; } = Asset.SteamAppID;
        public ulong ContextId { get; set; } = Asset.SteamCommunityContextID;
        public List<string> AssetIds { get; set; } = [];
        public List<string> OfferIds { get; set; } = [];
        public string State { get; set; } = "sending";
        public string? Message { get; set; }
        public string UpdatedAtUtc { get; set; } = "";
    }
}