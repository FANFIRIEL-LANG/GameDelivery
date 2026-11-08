using System.Globalization;
using System.Text.Json;
using System.Xml.Linq;

namespace GameDelivery.ASFPlugin;

internal static class ReadOnlyDiagnostic
{
    private const int MaxBadgeLevel = 5;

    internal static BadgeResult ParseBadge(string json, uint requestedAppId)
    {
        try
        {
            using JsonDocument document = JsonDocument.Parse(json);
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object
                || !TryGetInt32(root, "eresult", out int resultCode))
            {
                return BadgeResult.Failure("invalid-eresult");
            }

            if (resultCode != 1)
            {
                return BadgeResult.Failure("steam-error", resultCode);
            }

            if (!root.TryGetProperty("badgedata", out JsonElement badge)
                || badge.ValueKind != JsonValueKind.Object)
            {
                return BadgeResult.Failure("missing-badgedata", resultCode);
            }

            if (!TryGetUInt32(badge, "appid", out uint appId) || appId != requestedAppId)
            {
                return BadgeResult.Failure("appid-mismatch", resultCode);
            }

            if (!TryGetInt32(badge, "level", out int level) || level is < 0 or > MaxBadgeLevel)
            {
                return BadgeResult.Failure("invalid-level", resultCode);
            }

            if (!TryGetInt32(badge, "maxlevel", out int maxLevel) || maxLevel != MaxBadgeLevel)
            {
                return BadgeResult.Failure("invalid-maxlevel", resultCode);
            }

            if (!TryGetInt32(badge, "border", out int border) || border != 0)
            {
                return BadgeResult.Failure("invalid-border", resultCode);
            }

            if (!badge.TryGetProperty("rgCards", out JsonElement cards)
                || cards.ValueKind != JsonValueKind.Array)
            {
                return BadgeResult.Failure("invalid-cards", resultCode);
            }

            List<string> hashes = [];
            foreach (JsonElement card in cards.EnumerateArray())
            {
                if (card.ValueKind != JsonValueKind.Object
                    || !card.TryGetProperty("markethash", out JsonElement hashElement)
                    || hashElement.ValueKind != JsonValueKind.String
                    || string.IsNullOrWhiteSpace(hashElement.GetString()))
                {
                    return BadgeResult.Failure("invalid-card-hash", resultCode);
                }

                hashes.Add(hashElement.GetString()!);
            }

            string[] duplicates = hashes
                .GroupBy(hash => hash, StringComparer.Ordinal)
                .Where(group => group.Count() > 1)
                .Select(group => group.Key)
                .OrderBy(hash => hash, StringComparer.Ordinal)
                .ToArray();

            return new BadgeResult
            {
                Success = true,
                Error = null,
                EResult = resultCode,
                AppID = appId,
                Level = level,
                MaxLevel = maxLevel,
                Border = border,
                CardCount = hashes.Count,
                UniqueHashCount = hashes.Distinct(StringComparer.Ordinal).Count(),
                Hashes = hashes.ToArray(),
                DuplicateHashes = duplicates
            };
        }
        catch (JsonException)
        {
            return BadgeResult.Failure("invalid-json");
        }
    }

    internal static ProfileIdentityResult VerifyProfileIdentity(string xml, ulong requestedSteamId64)
    {
        if (requestedSteamId64 == 0 || string.IsNullOrWhiteSpace(xml))
        {
            return ProfileIdentityResult.Failure("invalid-request-or-profile");
        }

        try
        {
            XDocument document = XDocument.Parse(xml, LoadOptions.None);
            XElement[] identityElements = document
                .Descendants()
                .Where(element => string.Equals(element.Name.LocalName, "steamID64", StringComparison.Ordinal))
                .ToArray();

            if (identityElements.Length != 1
                || !ulong.TryParse(
                    identityElements[0].Value,
                    NumberStyles.None,
                    CultureInfo.InvariantCulture,
                    out ulong actualSteamId64)
                || actualSteamId64 == 0)
            {
                return ProfileIdentityResult.Failure("profile-identity-unavailable");
            }

            return actualSteamId64 == requestedSteamId64
                ? new ProfileIdentityResult(true, null, actualSteamId64)
                : ProfileIdentityResult.Failure("profile-identity-mismatch", actualSteamId64);
        }
        catch (System.Xml.XmlException)
        {
            return ProfileIdentityResult.Failure("invalid-profile-xml");
        }
    }

    internal static async Task<InventorySummary> AggregateInventoryAsync(
        IAsyncEnumerable<InventoryEntry> inventory)
    {
        ArgumentNullException.ThrowIfNull(inventory);

        Dictionary<uint, Dictionary<string, InventoryCardAccumulator>> cards = [];
        ulong foilAmount = 0;
        ulong otherTypeAmount = 0;
        ulong unknownTypeAmount = 0;
        ulong missingHashAmount = 0;
        ulong unknownAppAmount = 0;

        await foreach (InventoryEntry? item in inventory.ConfigureAwait(false))
        {
            if (item is null || item.Amount == 0)
            {
                unknownTypeAmount++;
                continue;
            }

            if (string.Equals(item.Type, "TradingCard", StringComparison.Ordinal))
            {
                if (item.RealAppID == 0)
                {
                    unknownAppAmount += item.Amount;
                }
                else if (string.IsNullOrWhiteSpace(item.MarketHashName))
                {
                    missingHashAmount += item.Amount;
                }
                else
                {
                    if (!cards.TryGetValue(item.RealAppID, out Dictionary<string, InventoryCardAccumulator>? appCards))
                    {
                        appCards = new Dictionary<string, InventoryCardAccumulator>(StringComparer.Ordinal);
                        cards.Add(item.RealAppID, appCards);
                    }

                    if (!appCards.TryGetValue(item.MarketHashName, out InventoryCardAccumulator? card))
                    {
                        card = new InventoryCardAccumulator();
                        appCards.Add(item.MarketHashName, card);
                    }

                    card.Amount += item.Amount;
                    if (!item.Tradable)
                    {
                        card.NonTradableAmount += item.Amount;
                    }
                }
            }
            else if (string.Equals(item.Type, "FoilTradingCard", StringComparison.Ordinal))
            {
                foilAmount += item.Amount;
            }
            else if (string.IsNullOrWhiteSpace(item.Type)
                || string.Equals(item.Type, "Unknown", StringComparison.Ordinal))
            {
                unknownTypeAmount += item.Amount;
            }
            else
            {
                otherTypeAmount += item.Amount;
            }
        }

        InventoryCardGroup[] groups = cards
            .SelectMany(app => app.Value.Select(card => new InventoryCardGroup(
                app.Key,
                card.Key,
                card.Value.Amount,
                card.Value.NonTradableAmount)))
            .OrderBy(card => card.RealAppID)
            .ThenBy(card => card.MarketHashName, StringComparer.Ordinal)
            .ToArray();

        return new InventorySummary(
            groups,
            foilAmount,
            otherTypeAmount,
            unknownTypeAmount,
            missingHashAmount,
            unknownAppAmount);
    }

    internal static MatchResult CompareBadgeToInventory(
        BadgeResult badge,
        InventorySummary inventory,
        uint requestedAppId)
    {
        if (!badge.Success || badge.AppID != requestedAppId)
        {
            return MatchResult.Failure("badge-data-unavailable");
        }

        if (inventory is null)
        {
            return MatchResult.Failure("inventory-unavailable");
        }

        HashSet<string> badgeHashes = new(badge.Hashes, StringComparer.Ordinal);
        Dictionary<string, ulong> inventoryCards = inventory.Cards
            .Where(card => card.RealAppID == requestedAppId)
            .ToDictionary(card => card.MarketHashName, card => card.Amount, StringComparer.Ordinal);
        HashSet<string> inventoryHashes = new(inventoryCards.Keys, StringComparer.Ordinal);

        return new MatchResult
        {
            Success = true,
            Error = null,
            MatchedHashes = badgeHashes.Intersect(inventoryHashes, StringComparer.Ordinal)
                .OrderBy(hash => hash, StringComparer.Ordinal)
                .ToArray(),
            MissingHashes = badgeHashes.Except(inventoryHashes, StringComparer.Ordinal)
                .OrderBy(hash => hash, StringComparer.Ordinal)
                .ToArray(),
            ExtraHashes = inventoryHashes.Except(badgeHashes, StringComparer.Ordinal)
                .OrderBy(hash => hash, StringComparer.Ordinal)
                .ToArray(),
            DuplicateBadgeHashes = badge.DuplicateHashes,
            DuplicateInventoryHashes = inventoryCards
                .Where(card => card.Value > 1)
                .Select(card => card.Key)
                .OrderBy(hash => hash, StringComparer.Ordinal)
                .ToArray()
        };
    }

    private static bool TryGetInt32(JsonElement element, string propertyName, out int value)
    {
        if (element.TryGetProperty(propertyName, out JsonElement property)
            && property.ValueKind == JsonValueKind.Number
            && property.TryGetInt32(out value))
        {
            return true;
        }

        value = 0;
        return false;
    }

    private static bool TryGetUInt32(JsonElement element, string propertyName, out uint value)
    {
        if (element.TryGetProperty(propertyName, out JsonElement property)
            && property.ValueKind == JsonValueKind.Number
            && property.TryGetUInt32(out value))
        {
            return true;
        }

        value = 0;
        return false;
    }

    internal sealed class BadgeResult
    {
        public bool Success { get; init; }
        public string? Error { get; init; }
        public int? EResult { get; init; }
        public uint? AppID { get; init; }
        public int? Level { get; init; }
        public int? MaxLevel { get; init; }
        public int? Border { get; init; }
        public int CardCount { get; init; }
        public int UniqueHashCount { get; init; }
        public string[] Hashes { get; init; } = [];
        public string[] DuplicateHashes { get; init; } = [];

        internal static BadgeResult Failure(string error, int? resultCode = null) => new()
        {
            Success = false,
            Error = error,
            EResult = resultCode
        };
    }

    internal sealed record ProfileIdentityResult(bool Verified, string? Error, ulong? ActualSteamID64)
    {
        internal static ProfileIdentityResult Failure(string error, ulong? actualSteamId64 = null) =>
            new(false, error, actualSteamId64);
    }

    internal sealed class InventoryEntry
    {
        public InventoryEntry(
            uint realAppId,
            string? marketHashName,
            string? type,
            uint amount,
            bool tradable)
        {
            RealAppID = realAppId;
            MarketHashName = marketHashName;
            Type = type;
            Amount = amount;
            Tradable = tradable;
        }

        public uint RealAppID { get; }
        public string? MarketHashName { get; }
        public string? Type { get; }
        public uint Amount { get; }
        public bool Tradable { get; }
    }

    internal sealed record InventoryCardGroup(
        uint RealAppID,
        string MarketHashName,
        ulong Amount,
        ulong NonTradableAmount);

    internal sealed record InventorySummary(
        InventoryCardGroup[] Cards,
        ulong FoilAmount,
        ulong OtherTypeAmount,
        ulong UnknownTypeAmount,
        ulong MissingHashAmount,
        ulong UnknownAppAmount);

    internal sealed class MatchResult
    {
        public bool Success { get; init; }
        public string? Error { get; init; }
        public string[] MatchedHashes { get; init; } = [];
        public string[] MissingHashes { get; init; } = [];
        public string[] ExtraHashes { get; init; } = [];
        public string[] DuplicateBadgeHashes { get; init; } = [];
        public string[] DuplicateInventoryHashes { get; init; } = [];

        internal static MatchResult Failure(string error) => new()
        {
            Success = false,
            Error = error
        };
    }

    private sealed class InventoryCardAccumulator
    {
        internal ulong Amount { get; set; }
        internal ulong NonTradableAmount { get; set; }
    }
}
