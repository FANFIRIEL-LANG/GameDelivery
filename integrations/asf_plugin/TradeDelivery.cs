using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
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
    private static readonly SemaphoreSlim SendSemaphore = new(1, 1);
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        PropertyNameCaseInsensitive = true
    };

    public static async Task<string> ProcessSendCommand(string encodedPayload)
    {
        SendRequest? request;
        try
        {
            string encoded = encodedPayload.Replace('-', '+').Replace('_', '/');
            encoded = encoded.PadRight(encoded.Length + ((4 - encoded.Length % 4) % 4), '=');
            request = JsonSerializer.Deserialize<SendRequest>(Convert.FromBase64String(encoded), JsonOptions);
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
            || !Regex.IsMatch(request.CredentialNonce ?? "", "^[a-f0-9]{64}$", RegexOptions.CultureInvariant))
        {
            return Failure("В запросе отправки отсутствуют обязательные поля или они некорректны.");
        }

        if (request.AssetIds.Any(string.IsNullOrWhiteSpace)
            || request.AssetIds.Distinct(StringComparer.Ordinal).Count() != request.AssetIds.Count
            || request.AssetIds.Any(value =>
                !ulong.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out ulong id) || id == 0))
        {
            return Failure("Список asset ID пустой, содержит повторы или некорректные значения.");
        }

        PendingTradeSecret? secret;
        string secretPath = GetPendingSecretPath(request.CredentialNonce);
        try
        {
            string json = await File.ReadAllTextAsync(secretPath).ConfigureAwait(false);
            secret = JsonSerializer.Deserialize<PendingTradeSecret>(json, JsonOptions);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or JsonException)
        {
            return Failure("Одноразовый секрет отправки отсутствует или не читается. Предложение не отправлено.");
        }
        finally
        {
            try { File.Delete(secretPath); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        }

        if (secret is null
            || !string.Equals(secret.CredentialNonce, request.CredentialNonce, StringComparison.Ordinal)
            || !string.Equals(secret.OrderId, request.OrderId, StringComparison.Ordinal)
            || !string.Equals(secret.BotName, request.BotName, StringComparison.OrdinalIgnoreCase)
            || secret.TargetSteamId64 != request.TargetSteamId64
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

        if (targetBot.SteamID == recipient)
        {
            return Failure("Нельзя отправлять предложение самому себе.");
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
        List<Asset> selectedAssets = new(request.AssetIds.Count);
        List<string> names = new(request.AssetIds.Count);
        foreach (string rawAssetId in request.AssetIds)
        {
            ulong assetId = ulong.Parse(rawAssetId, NumberStyles.None, CultureInfo.InvariantCulture);
            if (!byAssetId.TryGetValue(assetId, out Asset? asset)
                || asset.AppID != Asset.SteamAppID
                || asset.ContextID != Asset.SteamCommunityContextID
                || !asset.Tradable
                || !string.Equals(asset.Type.ToString(), "TradingCard", StringComparison.Ordinal))
            {
                return Failure("Актив " + rawAssetId + " не найден, не передаваем или не является обычной Steam-карточкой.");
            }

            selectedAssets.Add(asset);
            names.Add(asset.AssetID.ToString(CultureInfo.InvariantCulture));
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
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or JsonException)
            {
                return Failure("Нельзя прочитать журнал защиты от повторной отправки. Отправка заблокирована.");
            }

            if (records.TryGetValue(request.OrderId, out SendRecord? existing))
            {
                if (!string.Equals(existing.Fingerprint, fingerprint, StringComparison.Ordinal))
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

            SendRecord record = new()
            {
                OrderId = request.OrderId,
                Fingerprint = fingerprint,
                BotName = targetBot.BotName,
                TargetSteamId64 = request.TargetSteamId64,
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
                (bool success, HashSet<ulong>? tradeOfferIds, HashSet<ulong>? mobileTradeOfferIds) =
                    await targetBot.ArchiWebHandler.SendTradeOffer(
                        request.TargetSteamId64,
                        selectedAssets,
                        token: secret.TradeToken,
                        customMessage: "GameDelivery order " + request.OrderId,
                        forcedSingleOffer: true,
                        itemsPerTrade: (ushort)selectedAssets.Count).ConfigureAwait(false);

                HashSet<ulong> allOfferIds = new();
                if (tradeOfferIds is not null) allOfferIds.UnionWith(tradeOfferIds);
                if (mobileTradeOfferIds is not null) allOfferIds.UnionWith(mobileTradeOfferIds);
                if (allOfferIds.Count == 0)
                {
                    record.State = "unknown";
                    record.Message = "ASF did not return a trade offer ID";
                    record.UpdatedAtUtc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
                    SaveSendRecords(records);
                    return Failure(
                        "ASF не вернул ID предложения. Результат неизвестен; проверь исходящие предложения Steam и не повторяй отправку.",
                        resultUnknown: true);
                }

                record.OfferIds = allOfferIds.Order().Select(id => id.ToString(CultureInfo.InvariantCulture)).ToList();
                bool confirmationPending = mobileTradeOfferIds?.Count > 0;
                if (confirmationPending && targetBot.HasMobileAuthenticator)
                {
                    (bool twoFactorSuccess, _, string confirmationMessage) =
                        await targetBot.Actions.HandleTwoFactorAuthenticationConfirmations(
                            true, EMobileConfirmationType.Trade, mobileTradeOfferIds, true).ConfigureAwait(false);
                    confirmationPending = !twoFactorSuccess;
                    record.Message = SafeMessage(confirmationMessage);
                }

                record.State = confirmationPending ? "awaiting_confirmation" : "sent";
                record.UpdatedAtUtc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
                SaveSendRecords(records);
                return SerializeRecord(record, duplicatePrevented: false, sendSucceeded: success);
            }
            catch (Exception exception)
            {
                record.State = "unknown";
                record.Message = "Exception while sending; do not retry automatically";
                record.UpdatedAtUtc = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture);
                try { SaveSendRecords(records); } catch (IOException) { } catch (UnauthorizedAccessException) { }
                return Failure(
                    "Не удалось однозначно подтвердить результат отправки. Проверь исходящие предложения Steam; повторная отправка заблокирована.",
                    resultUnknown: true);
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
            string.Join(",", request.AssetIds.OrderBy(value => value, StringComparer.Ordinal)));
        return Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(canonical)));
    }

    private static Dictionary<string, SendRecord> LoadSendRecords()
    {
        string path = GetSendRecordPath();
        if (!File.Exists(path))
        {
            return new Dictionary<string, SendRecord>(StringComparer.OrdinalIgnoreCase);
        }

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
        string temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            File.WriteAllText(temporary, JsonSerializer.Serialize(records, JsonOptions), new UTF8Encoding(false));
            File.Move(temporary, path, overwrite: true);
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

    private static string Failure(string message, bool resultUnknown = false) => JsonSerializer.Serialize(new
    {
        success = false,
        mode = "send",
        sendEnabled = true,
        resultUnknown,
        error = message
    }, JsonOptions);

    private static string SafeMessage(string? message)
    {
        if (string.IsNullOrWhiteSpace(message))
        {
            return "не удалось завершить подтверждение в мобильном приложении";
        }

        return new string(message.Where(character => !char.IsControl(character)).Take(180).ToArray());
    }

    private sealed class SendRequest
    {
        public string OrderId { get; set; } = "";
        public string BotName { get; set; } = "";
        public ulong TargetSteamId64 { get; set; }
        public List<string> AssetIds { get; set; } = [];
        public string CredentialNonce { get; set; } = "";
    }

    private sealed class PendingTradeSecret
    {
        public string CredentialNonce { get; set; } = "";
        public string OrderId { get; set; } = "";
        public string BotName { get; set; } = "";
        public ulong TargetSteamId64 { get; set; }
        public List<string> AssetIds { get; set; } = [];
        public string TradeToken { get; set; } = "";
    }

    private sealed class SendRecord
    {
        public string OrderId { get; set; } = "";
        public string Fingerprint { get; set; } = "";
        public string BotName { get; set; } = "";
        public ulong TargetSteamId64 { get; set; }
        public List<string> AssetIds { get; set; } = [];
        public List<string> OfferIds { get; set; } = [];
        public string State { get; set; } = "sending";
        public string? Message { get; set; }
        public string UpdatedAtUtc { get; set; } = "";
    }
}