using ArchiSteamFarm.Plugins.Interfaces;
using GameDelivery.ASFPlugin;
using System;
using System.Composition;
using System.Composition.Convention;
using System.Composition.Hosting;
using System.Linq;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization.Metadata;

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

static bool TryNormalizeFromBuiltPlugin(
    string message,
    string[] args,
    string commandPrefix,
    out string[] normalizedArgs)
{
    Type parserType = typeof(GameDeliveryPlugin).Assembly.GetType(
        "GameDelivery.ASFPlugin.CommandArgumentParser",
        throwOnError: true)!;
    MethodInfo method = parserType.GetMethod(
        "TryNormalize",
        BindingFlags.Public | BindingFlags.Static)
        ?? throw new InvalidOperationException("Built plugin command parser method was not found");
    object?[] parameters = [message, args, commandPrefix, null];
    bool result = (bool)(method.Invoke(null, parameters) ?? false);
    normalizedArgs = parameters[3] as string[] ?? [];
    return result;
}

string[] tokens;

Assert(
    TryNormalizeFromBuiltPlugin(
        "gamedelivery-diagnose Ewaylariell",
        ["gamedelivery-diagnose", "Ewaylariell"],
        "!",
        out tokens),
    "plain ASF console command was not recognized");
Assert(tokens.SequenceEqual(["gamedelivery-diagnose", "Ewaylariell"]), "plain command arguments changed");

Assert(
    TryNormalizeFromBuiltPlugin(
        "!gamedelivery-diagnose Ewaylariell",
        ["!gamedelivery-diagnose", "Ewaylariell"],
        "!",
        out tokens),
    "prefixed command was not recognized");
Assert(tokens.SequenceEqual(["gamedelivery-diagnose", "Ewaylariell"]), "prefix was not stripped exactly");

Assert(
    TryNormalizeFromBuiltPlugin(
        "gamedelivery-diagnose Ewaylariell",
        ["unparsed"],
        "!",
        out tokens),
    "raw-message fallback was not used when args did not contain the custom command");
Assert(tokens.SequenceEqual(["gamedelivery-diagnose", "Ewaylariell"]), "raw-message fallback changed command arguments");

Assert(
    TryNormalizeFromBuiltPlugin(
        "gamedelivery-preview eyJvcmRlcklkIjoiMSJ9",
        ["gamedelivery-preview", "eyJvcmRlcklkIjoiMSJ9"],
        "!",
        out tokens),
    "preview payload command was not recognized");
Assert(tokens.Length == 2 && tokens[1] == "eyJvcmRlcklkIjoiMSJ9", "preview payload was modified");

Assert(
    !TryNormalizeFromBuiltPlugin("status gamedelivery-diagnose", ["status", "gamedelivery-diagnose"], "!", out _),
    "custom command token inside an unrelated ASF command was intercepted");

Assert(typeof(IPlugin).IsAssignableFrom(typeof(GameDeliveryPlugin)),
    "plugin does not implement the exact IPlugin interface from the pinned ASF assembly");
Assert(typeof(IASF).IsAssignableFrom(typeof(GameDeliveryPlugin)),
    "plugin does not implement the exact IASF interface from the pinned ASF assembly");
Assert(typeof(IBotCommand2).IsAssignableFrom(typeof(GameDeliveryPlugin)),
    "plugin does not implement the exact IBotCommand2 interface from the pinned ASF assembly");
Assert(typeof(GameDeliveryPlugin).GetInterfaceMap(typeof(IBotCommand2)).TargetMethods
        .Any(method => method.Name == "OnBotCommand"),
    "IBotCommand2.OnBotCommand has no implementation mapping");
Assert(typeof(GameDeliveryPlugin).GetCustomAttributesData()
        .Any(attribute => attribute.AttributeType.FullName == "System.Composition.ExportAttribute"),
    "plugin has no MEF Export attribute");

ConventionBuilder conventions = new();
conventions.ForTypesDerivedFrom<IPlugin>().Export<IPlugin>();
using CompositionHost container = new ContainerConfiguration()
    .WithAssemblies([typeof(GameDeliveryPlugin).Assembly], conventions)
    .CreateContainer();
IPlugin[] exports = container.GetExports<IPlugin>().ToArray();
GameDeliveryPlugin[] gameDeliveryExports = exports.OfType<GameDeliveryPlugin>().ToArray();
Assert(gameDeliveryExports.Length == 1,
    "ASF-style MEF composition did not discover exactly one GameDeliveryPlugin");
Assert(exports.OfType<IBotCommand2>().Count() == 1,
    "ASF-style MEF composition did not expose the plugin as an IBotCommand2 handler");

Assert(typeof(GameDeliveryPlugin).Assembly.GetName().Version == new Version(1, 0, 2, 0),
    "plugin assembly version is not the expected fixed version 1.0.2.0");

static void InvokeStoragePermissionRestriction(string path, bool isDirectory)
{
    Type tradeDeliveryType = typeof(GameDeliveryPlugin).Assembly.GetType(
        "GameDelivery.ASFPlugin.TradeDelivery",
        throwOnError: true)!;
    MethodInfo method = tradeDeliveryType.GetMethod(
        "RestrictStoragePermissions",
        BindingFlags.NonPublic | BindingFlags.Static)
        ?? throw new InvalidOperationException("Built plugin storage ACL method was not found");

    try
    {
        method.Invoke(null, [path, isDirectory]);
    }
    catch (TargetInvocationException exception)
    {
        throw new InvalidOperationException(
            "The built plugin storage ACL method failed.",
            exception.InnerException ?? exception);
    }
}

static void VerifyWindowsJournalAclWithExclusiveFileHandle()
{
    if (!OperatingSystem.IsWindows())
    {
        return;
    }

    string directory = System.IO.Path.Combine(
        System.IO.Path.GetTempPath(),
        "GameDelivery-ACL-" + Guid.NewGuid().ToString("N"));
    System.IO.Directory.CreateDirectory(directory);

    try
    {
        InvokeStoragePermissionRestriction(directory, isDirectory: true);
        string journalPath = System.IO.Path.Combine(directory, "sent_offers.json.tmp");

        using System.IO.FileStream stream = new(
            journalPath,
            System.IO.FileMode.CreateNew,
            System.IO.FileAccess.Write,
            System.IO.FileShare.None);

        // This mirrors SaveSendRecords: the ACL is applied after FileStream opens
        // the temporary file exclusively and before any journal bytes are written.
        InvokeStoragePermissionRestriction(journalPath, isDirectory: false);
        stream.WriteByte((byte)'{');
        stream.Flush(flushToDisk: true);
    }
    finally
    {
        System.IO.Directory.Delete(directory, recursive: true);
    }
}

VerifyWindowsJournalAclWithExclusiveFileHandle();

static string InvokeSafeMessageSanitizer(string message, string tradeToken)
{
    Type tradeDeliveryType = typeof(GameDeliveryPlugin).Assembly.GetType(
        "GameDelivery.ASFPlugin.TradeDelivery",
        throwOnError: true)!;
    MethodInfo method = tradeDeliveryType.GetMethod(
        "SafeMessage",
        BindingFlags.NonPublic | BindingFlags.Static)
        ?? throw new InvalidOperationException("Built plugin confirmation sanitizer was not found");

    try
    {
        return (string)(method.Invoke(null, [message, tradeToken]) ?? "");
    }
    catch (TargetInvocationException exception)
    {
        throw new InvalidOperationException(
            "The built plugin confirmation sanitizer failed.",
            exception.InnerException ?? exception);
    }
}

string sanitizedConfirmation = InvokeSafeMessageSanitizer(
    "Steam confirmation failed for token=SensitiveTradeToken_123",
    "SensitiveTradeToken_123");
Assert(!sanitizedConfirmation.Contains("SensitiveTradeToken_123", StringComparison.Ordinal),
    "mobile confirmation text exposed the Steam trade token");
Assert(!sanitizedConfirmation.Contains("token=SensitiveTradeToken_123", StringComparison.Ordinal),
    "mobile confirmation text retained a token query parameter");

static string InvokePluginDiagnosticSanitizer(string message)
{
    MethodInfo method = typeof(GameDeliveryPlugin).GetMethod(
        "SafeDiagnosticText",
        BindingFlags.NonPublic | BindingFlags.Static)
        ?? throw new InvalidOperationException("Built plugin diagnostic sanitizer was not found");

    try
    {
        return (string)(method.Invoke(null, [message]) ?? "");
    }
    catch (TargetInvocationException exception)
    {
        throw new InvalidOperationException(
            "The built plugin diagnostic sanitizer failed.",
            exception.InnerException ?? exception);
    }
}

string safeDiagnostic = InvokePluginDiagnosticSanitizer(
    "MissingMethodException: token=NeverLogThisTradeToken&next=value\n at GameDeliveryPlugin.Preview");
Assert(!safeDiagnostic.Contains("NeverLogThisTradeToken", StringComparison.Ordinal),
    "diagnostic sanitizer exposed a Steam token");
Assert(safeDiagnostic.Contains("token=[REDACTED]&next=value", StringComparison.Ordinal),
    "diagnostic sanitizer did not preserve the error context while redacting the token");
Assert(!safeDiagnostic.Contains((char)10) && !safeDiagnostic.Contains((char)13),
    "diagnostic sanitizer retained control characters");

static void VerifyReflectionDisabledJsonResolver()
{
    Assert(!JsonSerializer.IsReflectionEnabledByDefault,
        "JSON smoke test must run with reflection-based serialization disabled");

    JsonSerializerOptions ReadPluginOptions(Type owner)
    {
        FieldInfo field = owner.GetField("JsonOptions", BindingFlags.NonPublic | BindingFlags.Static)
            ?? throw new InvalidOperationException($"JSON options were not found on {owner.FullName}");
        return field.GetValue(null) as JsonSerializerOptions
            ?? throw new InvalidOperationException($"JSON options were not initialized on {owner.FullName}");
    }

    Type tradeType = typeof(GameDeliveryPlugin).Assembly.GetType(
        "GameDelivery.ASFPlugin.TradeDelivery", throwOnError: true)!;
    JsonSerializerOptions[] optionSets = [
        ReadPluginOptions(typeof(GameDeliveryPlugin)),
        ReadPluginOptions(tradeType)
    ];

    foreach (JsonSerializerOptions options in optionSets)
    {
        Assert(options.TypeInfoResolver is DefaultJsonTypeInfoResolver,
            "plugin JSON options do not explicitly configure DefaultJsonTypeInfoResolver");
        string json = JsonSerializer.Serialize(new SmokeJsonProbe
        {
            OrderId = "GD-JSON-REFLECTION-OFF",
            AssetIds = ["100", "200"]
        }, options);
        SmokeJsonProbe? roundTrip = JsonSerializer.Deserialize<SmokeJsonProbe>(json, options);
        Assert(roundTrip is not null
            && roundTrip.OrderId == "GD-JSON-REFLECTION-OFF"
            && roundTrip.AssetIds.SequenceEqual(["100", "200"]),
            "plugin JSON options failed serialize/deserialize with reflection default disabled");
    }
}

VerifyReflectionDisabledJsonResolver();

static bool InvokeSelfTradeGuard(ulong botSteamId64, ulong recipientSteamId64)
{
    Type tradeType = typeof(GameDeliveryPlugin).Assembly.GetType(
        "GameDelivery.ASFPlugin.TradeDelivery",
        throwOnError: true)!;
    MethodInfo method = tradeType.GetMethod(
        "IsSameSteamAccount",
        BindingFlags.NonPublic | BindingFlags.Static)
        ?? throw new InvalidOperationException("Built plugin SteamID64 self-trade guard was not found");

    try
    {
        return (bool)(method.Invoke(null, [botSteamId64, recipientSteamId64]) ?? false);
    }
    catch (TargetInvocationException exception)
    {
        throw new InvalidOperationException(
            "The built plugin SteamID64 self-trade guard failed.",
            exception.InnerException ?? exception);
    }
}

Assert(
    InvokeSelfTradeGuard(76561197960265729UL, 76561197960265729UL),
    "SteamID64 guard did not detect a self-trade");
Assert(
    !InvokeSelfTradeGuard(76561197960265729UL, 76561197960265730UL),
    "SteamID64 guard rejected a different recipient");

static object? GetProperty(object value, string name) =>
    value.GetType().GetProperty(name, BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance)
        ?.GetValue(value);

static object InvokeReadOnlyDiagnostic(string methodName, params object?[] arguments)
{
    Type helperType = typeof(GameDeliveryPlugin).Assembly.GetType(
        "GameDelivery.ASFPlugin.ReadOnlyDiagnostic",
        throwOnError: true)!;
    MethodInfo method = helperType.GetMethod(
        methodName,
        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static)
        ?? throw new InvalidOperationException($"Read-only diagnostic method {methodName} was not found");

    try
    {
        return method.Invoke(null, arguments)
            ?? throw new InvalidOperationException($"Read-only diagnostic method {methodName} returned null");
    }
    catch (TargetInvocationException exception)
    {
        throw new InvalidOperationException(
            $"Read-only diagnostic method {methodName} failed.",
            exception.InnerException ?? exception);
    }
}

static string InvokeDiagnosticFailure(string message)
{
    MethodInfo method = typeof(GameDeliveryPlugin).GetMethod(
        "DiagnosticFailure",
        BindingFlags.NonPublic | BindingFlags.Static)
        ?? throw new InvalidOperationException("Built plugin diagnostic failure serializer was not found");

    try
    {
        return (string)(method.Invoke(null, [message])
            ?? throw new InvalidOperationException("Diagnostic failure serializer returned null"));
    }
    catch (TargetInvocationException exception)
    {
        throw new InvalidOperationException(
            "Built plugin diagnostic failure serializer failed.",
            exception.InnerException ?? exception);
    }
}

static bool TryParseDiagnosticArguments(
    string[] commandArgs,
    out uint[] appIds,
    out ulong? recipientSteamId64,
    out string error)
{
    MethodInfo method = typeof(GameDeliveryPlugin).GetMethod(
        "TryParseDiagnosticArguments",
        BindingFlags.NonPublic | BindingFlags.Static)
        ?? throw new InvalidOperationException("Built plugin diagnostic argument parser was not found");
    object?[] parameters = [commandArgs, null, null, null];
    bool success = (bool)(method.Invoke(null, parameters) ?? false);
    appIds = parameters[1] as uint[] ?? [];
    recipientSteamId64 = parameters[2] is ulong id ? id : null;
    error = Convert.ToString(parameters[3]) ?? "";
    return success;
}

static bool GetBool(object value, string property) => GetProperty(value, property) is true;
static int GetInt(object value, string property) => Convert.ToInt32(GetProperty(value, property));

static string[] GetStrings(object value, string property) =>
    ((System.Collections.IEnumerable?)GetProperty(value, property))
        ?.Cast<object>()
        .Select(item => Convert.ToString(item) ?? "")
        .ToArray()
        ?? [];

static object CreateInventoryEntry(
    Type entryType,
    uint realAppId,
    string? marketHashName,
    string type,
    uint amount,
    bool tradable)
{
    return Activator.CreateInstance(entryType, realAppId, marketHashName, type, amount, tradable)
        ?? throw new InvalidOperationException("Read-only diagnostic inventory entry could not be created");
}

static async Task<object> AggregateInventoryPages(Type entryType, object[][] pageEntries)
{
    Array pages = Array.CreateInstance(entryType.MakeArrayType(), pageEntries.Length);
    for (int pageIndex = 0; pageIndex < pageEntries.Length; pageIndex++)
    {
        Array page = Array.CreateInstance(entryType, pageEntries[pageIndex].Length);
        for (int itemIndex = 0; itemIndex < pageEntries[pageIndex].Length; itemIndex++)
        {
            page.SetValue(pageEntries[pageIndex][itemIndex], itemIndex);
        }

        pages.SetValue(page, pageIndex);
    }

    MethodInfo yieldMethod = typeof(SmokeInventoryPageFactory).GetMethod(
        nameof(SmokeInventoryPageFactory.Yield),
        BindingFlags.Public | BindingFlags.Static)!
        .MakeGenericMethod(entryType);
    object items = yieldMethod.Invoke(null, [pages])!;
    object task = InvokeReadOnlyDiagnostic("AggregateInventoryAsync", items);
    await ((Task)task).ConfigureAwait(false);
    return GetProperty(task, "Result")!;
}

static async Task VerifyReadOnlyDiagnosticRules()
{
    using (JsonDocument failure = JsonDocument.Parse(InvokeDiagnosticFailure("validation error")))
    {
        JsonElement failureRoot = failure.RootElement;
        Assert(failureRoot.GetProperty("success").GetBoolean() == false,
            "diagnostic failure did not return success=false");
        Assert(failureRoot.GetProperty("mode").GetString() == "diagnostic-read-only",
            "diagnostic failure omitted its read-only mode");
        Assert(failureRoot.GetProperty("sendEnabled").GetBoolean() == false,
            "diagnostic failure enabled sending");
        Assert(failureRoot.GetProperty("tradeOfferCreated").GetBoolean() == false,
            "diagnostic failure did not state that no trade offer was created");
    }

    Assert(TryParseDiagnosticArguments(
            ["gamedelivery-diagnose", "Bot", "440", "570", "--recipient", "76561197960265729"],
            out uint[] parsedAppIds,
            out ulong? parsedRecipient,
            out _)
        && parsedAppIds.SequenceEqual([440U, 570U])
        && parsedRecipient == 76561197960265729UL,
        "valid AppID and individual recipient diagnostic arguments were rejected");
    Assert(TryParseDiagnosticArguments(
            ["gamedelivery-diagnose", "Bot", "440"],
            out parsedAppIds,
            out parsedRecipient,
            out _)
        && parsedAppIds.SequenceEqual([440U])
        && parsedRecipient is null,
        "self badge diagnostic arguments were rejected");
    foreach (string[] invalidArguments in new[]
    {
        new[] { "gamedelivery-diagnose", "Bot", "0440" },
        new[] { "gamedelivery-diagnose", "Bot", "440", "440" },
        new[] { "gamedelivery-diagnose", "Bot", "440", "--recipient" },
        new[] { "gamedelivery-diagnose", "Bot", "440", "--recipient", "1" }
    })
    {
        Assert(!TryParseDiagnosticArguments(invalidArguments, out _, out _, out _),
            "non-canonical, repeated, or invalid diagnostic arguments were accepted");
    }

    object validBadge = InvokeReadOnlyDiagnostic(
        "ParseBadge",
        "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":3,\"maxlevel\":5,\"border\":0,\"rgCards\":[{\"markethash\":\"Card A\"},{\"markethash\":\"Card B\"}]}}",
        440U);
    Assert(GetBool(validBadge, "Success"), "valid badge payload was rejected");
    Assert(GetInt(validBadge, "Level") == 3, "badge level was not parsed");
    Assert(GetInt(validBadge, "CardCount") == 2, "badge card count was not parsed");
    Assert(GetInt(validBadge, "UniqueHashCount") == 2, "unique badge hash count was incorrect");

    foreach (string invalidBadge in new[]
    {
        "{",
        "{\"eresult\":15,\"badgedata\":{}}",
        "{\"eresult\":987654,\"badgedata\":{}}",
        "{\"eresult\":1}",
        "{\"eresult\":1,\"badgedata\":{\"appid\":441,\"level\":3,\"maxlevel\":5,\"border\":0,\"rgCards\":[]}}",
        "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":6,\"maxlevel\":5,\"border\":0,\"rgCards\":[]}}",
        "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":3,\"maxlevel\":4,\"border\":0,\"rgCards\":[]}}",
        "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":3,\"maxlevel\":5,\"border\":2,\"rgCards\":[]}}",
        "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":3,\"maxlevel\":5,\"border\":0,\"rgCards\":[{}]}}"
    })
    {
        object rejected = InvokeReadOnlyDiagnostic("ParseBadge", invalidBadge, 440U);
        Assert(!GetBool(rejected, "Success"), "malformed or unsupported badge payload was accepted");
        Assert(GetProperty(rejected, "Level") is null, "invalid badge payload was coerced into a level");
    }

    object emptyBadge = InvokeReadOnlyDiagnostic(
        "ParseBadge",
        "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":5,\"maxlevel\":5,\"border\":0,\"rgCards\":[]}}",
        440U);
    Assert(GetBool(emptyBadge, "Success") && GetInt(emptyBadge, "CardCount") == 0,
        "empty badge card set was not reported as a valid empty set");

    object duplicateBadge = InvokeReadOnlyDiagnostic(
        "ParseBadge",
        "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":2,\"maxlevel\":5,\"border\":0,\"rgCards\":[{\"markethash\":\"Card A\"},{\"markethash\":\"Card A\"}]}}",
        440U);
    Assert(GetBool(duplicateBadge, "Success") && GetInt(duplicateBadge, "CardCount") == 2,
        "duplicate badge hashes were silently discarded");
    Assert(GetInt(duplicateBadge, "UniqueHashCount") == 1
        && GetStrings(duplicateBadge, "DuplicateHashes").SequenceEqual(["Card A"]),
        "duplicate badge hash was not diagnosed");

    object foilBadge = InvokeReadOnlyDiagnostic(
        "ParseBadge",
        "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":1,\"maxlevel\":5,\"border\":1,\"rgCards\":[{\"markethash\":\"Foil\"}]}}",
        440U);
    Assert(GetBool(foilBadge, "Success") && GetInt(foilBadge, "Border") == 1,
        "valid foil badge metadata was not identified");

    object identityMatch = InvokeReadOnlyDiagnostic(
        "VerifyProfileIdentity",
        "<profile><steamID64>76561197960265729</steamID64></profile>",
        76561197960265729UL);
    Assert(GetBool(identityMatch, "Verified"), "matching Steam profile identity was rejected");
    object htmlNormalizedIdentityMatch = InvokeReadOnlyDiagnostic(
        "VerifyProfileIdentity",
        "<html><body><profile><steamid64>76561197960265729</steamid64></profile></body></html>",
        76561197960265729UL);
    Assert(GetBool(htmlNormalizedIdentityMatch, "Verified"),
        "HTML parser lower-cased the Steam identity element and the profile was rejected");
    Assert((bool)InvokeReadOnlyDiagnostic(
            "IsRecipientBadgeResponse",
            new Uri("https://steamcommunity.com/profiles/76561197960265729/ajaxgetbadgeinfo/440"),
            76561197960265729UL,
            440U),
        "numeric recipient badge response URI was rejected");
    foreach (string wrongRecipientBadgeUrl in new[]
    {
        "https://steamcommunity.com/profiles/76561197960265730/ajaxgetbadgeinfo/440",
        "https://steamcommunity.com/profiles/76561197960265729/ajaxgetbadgeinfo/441",
        "https://example.com/profiles/76561197960265729/ajaxgetbadgeinfo/440",
        "http://steamcommunity.com/profiles/76561197960265729/ajaxgetbadgeinfo/440"
    })
    {
        Assert(!(bool)InvokeReadOnlyDiagnostic(
                "IsRecipientBadgeResponse",
                new Uri(wrongRecipientBadgeUrl),
                76561197960265729UL,
                440U),
            "redirected recipient badge response was attributed to the requested SteamID64");
    }
    foreach (string invalidProfile in new[]
    {
        "<profile><steamID64>76561197960265730</steamID64></profile>",
        "<profile><steamID64>bad-id</steamID64></profile>",
        "<profile></profile>",
        "<profile>"
    })
    {
        object identityFailure = InvokeReadOnlyDiagnostic(
            "VerifyProfileIdentity",
            invalidProfile,
            76561197960265729UL);
        Assert(!GetBool(identityFailure, "Verified"), "missing, malformed, or mismatched profile identity was accepted");
    }

    Type entryType = typeof(GameDeliveryPlugin).Assembly.GetType(
        "GameDelivery.ASFPlugin.ReadOnlyDiagnostic+InventoryEntry",
        throwOnError: true)!;
    MethodInfo failingInventoryFactory = typeof(SmokeInventoryPageFactory).GetMethod(
        nameof(SmokeInventoryPageFactory.FailAfterOne),
        BindingFlags.Public | BindingFlags.Static)!
        .MakeGenericMethod(entryType);
    object failingInventory = failingInventoryFactory.Invoke(null,
    [
        CreateInventoryEntry(entryType, 440U, "Card before failure", "TradingCard", 1U, true)
    ])!;
    object failedAggregation = InvokeReadOnlyDiagnostic("AggregateInventoryAsync", failingInventory);
    bool inventoryFailurePropagated = false;
    try
    {
        await ((Task)failedAggregation).ConfigureAwait(false);
    }
    catch (InvalidOperationException exception)
        when (exception.Message == SmokeInventoryPageFactory.ExpectedFailureMessage)
    {
        inventoryFailurePropagated = true;
    }

    Assert(inventoryFailurePropagated,
        "an ASF inventory enumeration error was swallowed instead of failing the diagnostic");

    object inventory = await AggregateInventoryPages(entryType,
    [
        [
            CreateInventoryEntry(entryType, 440U, "Card A", "TradingCard", 2U, false),
            CreateInventoryEntry(entryType, 440U, "Card B", "TradingCard", 1U, true),
            CreateInventoryEntry(entryType, 440U, "Inventory Extra", "TradingCard", 1U, true),
            CreateInventoryEntry(entryType, 441U, "Other App", "TradingCard", 1U, true),
            CreateInventoryEntry(entryType, 440U, "Foil", "FoilTradingCard", 1U, true)
        ],
        [
            CreateInventoryEntry(entryType, 440U, "Unknown", "Unknown", 1U, true),
            CreateInventoryEntry(entryType, 440U, "Emoticon", "Emoticon", 1U, true),
            CreateInventoryEntry(entryType, 440U, null, "TradingCard", 1U, false),
            CreateInventoryEntry(entryType, 440U, "case-sensitive", "TradingCard", 1U, true),
            CreateInventoryEntry(entryType, 440U, "Invalid amount", "TradingCard", 0U, true)
        ]
    ]);

    object? cardGroups = GetProperty(inventory, "Cards");
    Assert(cardGroups is System.Collections.IEnumerable,
        "inventory card groups were unavailable on " + inventory.GetType().FullName
        + "; properties=" + string.Join(",", inventory.GetType().GetProperties(
            BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance).Select(property => property.Name)));
    object[] groups = ((System.Collections.IEnumerable)cardGroups!)
        .Cast<object>()
        .ToArray();
    object? cardA = groups.Single(group => (uint)GetProperty(group, "RealAppID")! == 440U
        && string.Equals((string?)GetProperty(group, "MarketHashName"), "Card A", StringComparison.Ordinal));
    Assert(cardA is not null && GetInt(cardA, "Amount") == 2, "inventory amount was not aggregated across async pages");
    Assert(GetInt(cardA!, "NonTradableAmount") == 2, "non-tradable buyer cards were excluded");
    Assert(GetInt(inventory, "FoilAmount") == 1, "foil cards were counted as normal cards");
    Assert(GetInt(inventory, "UnknownTypeAmount") == 1, "unknown inventory types were not counted separately");
    Assert(GetInt(inventory, "OtherTypeAmount") == 1, "non-card inventory types were not counted separately");
    Assert(GetInt(inventory, "MissingHashAmount") == 1, "normal card without a hash was not diagnosed");
    Assert(GetInt(inventory, "InvalidAmountCount") == 1,
        "a zero-amount card was misreported as an unknown inventory type");
    JsonSerializerOptions pluginOptions = (JsonSerializerOptions)(typeof(GameDeliveryPlugin)
        .GetField("JsonOptions", BindingFlags.NonPublic | BindingFlags.Static)!
        .GetValue(null)!);
    using (JsonDocument inventoryJson = JsonDocument.Parse(JsonSerializer.Serialize(inventory, pluginOptions)))
    {
        Assert(inventoryJson.RootElement.GetProperty("cards").GetArrayLength() > 0,
            "inventory summary could not be serialized into the diagnostic response");
    }

    object comparison = InvokeReadOnlyDiagnostic("CompareBadgeToInventory", duplicateBadge, inventory, 440U);
    Assert(GetStrings(comparison, "MatchedHashes").SequenceEqual(["Card A"]),
        "exact badge/inventory hash match was not found");
    Assert(GetStrings(comparison, "MissingHashes").Length == 0,
        "an owned badge hash was reported missing");
    Assert(GetStrings(comparison, "ExtraHashes").Contains("Inventory Extra", StringComparer.Ordinal),
        "inventory-only hash was not reported as extra");
    Assert(!GetStrings(comparison, "ExtraHashes").Contains("Foil", StringComparer.Ordinal)
        && !GetStrings(comparison, "ExtraHashes").Contains("Other App", StringComparer.Ordinal),
        "foil or another app's cards entered the normal card comparison");
    Assert(GetStrings(comparison, "DuplicateBadgeHashes").SequenceEqual(["Card A"]),
        "badge duplicates were not included in comparison results");
    Assert(GetStrings(comparison, "DuplicateInventoryHashes").SequenceEqual(["Card A"]),
        "multiple copies of an inventory hash were not reported");
    object foilComparison = InvokeReadOnlyDiagnostic("CompareBadgeToInventory", foilBadge, inventory, 440U);
    Assert(!GetBool(foilComparison, "Success")
        && (string?)GetProperty(foilComparison, "Error") == "foil-badge-excluded-from-normal-card-matching",
        "foil badge metadata was matched against normal recipient cards");

    object missingHashComparison = InvokeReadOnlyDiagnostic(
        "CompareBadgeToInventory",
        InvokeReadOnlyDiagnostic(
            "ParseBadge",
            "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":1,\"maxlevel\":5,\"border\":0,\"rgCards\":[{\"markethash\":\"Missing Card\"}]}}",
            440U),
        inventory,
        440U);
    Assert(GetStrings(missingHashComparison, "MissingHashes").SequenceEqual(["Missing Card"]),
        "badge card absent from the recipient inventory was not reported");

    object exactCaseComparison = InvokeReadOnlyDiagnostic(
        "CompareBadgeToInventory",
        InvokeReadOnlyDiagnostic(
            "ParseBadge",
            "{\"eresult\":1,\"badgedata\":{\"appid\":440,\"level\":1,\"maxlevel\":5,\"border\":0,\"rgCards\":[{\"markethash\":\"Case-Sensitive\"}]}}",
            440U),
        inventory,
        440U);
    Assert(GetStrings(exactCaseComparison, "MissingHashes").SequenceEqual(["Case-Sensitive"])
        && GetStrings(exactCaseComparison, "ExtraHashes").Contains("case-sensitive", StringComparer.Ordinal),
        "hash matching was not exact and case-sensitive");
}

await VerifyReadOnlyDiagnosticRules();

Console.WriteLine("ASF plugin command dispatch, MEF registration, and runtime compatibility smoke tests passed.");

sealed class SmokeJsonProbe
{
    public string OrderId { get; set; } = "";
    public string[] AssetIds { get; set; } = [];
}

static class SmokeInventoryPageFactory
{
    public const string ExpectedFailureMessage = "simulated ASF inventory failure";

    public static async IAsyncEnumerable<T> Yield<T>(T[][] pages)
    {
        foreach (T[] page in pages)
        {
            await Task.Yield();
            foreach (T item in page)
            {
                yield return item;
            }
        }
    }

    public static async IAsyncEnumerable<T> FailAfterOne<T>(T item)
    {
        await Task.Yield();
        yield return item;
        await Task.Yield();
        throw new InvalidOperationException(ExpectedFailureMessage);
    }
}
