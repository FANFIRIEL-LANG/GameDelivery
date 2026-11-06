using ArchiSteamFarm.Plugins.Interfaces;
using GameDelivery.ASFPlugin;
using System;
using System.Composition;
using System.Composition.Convention;
using System.Composition.Hosting;
using System.Linq;
using System.Reflection;

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
    "MissingMethodException: token=NeverLogThisTradeToken&next=value\\n at GameDeliveryPlugin.Preview");
Assert(!safeDiagnostic.Contains("NeverLogThisTradeToken", StringComparison.Ordinal),
    "diagnostic sanitizer exposed a Steam token");
Assert(safeDiagnostic.Contains("token=[REDACTED]&next=value", StringComparison.Ordinal),
    "diagnostic sanitizer did not preserve the error context while redacting the token");
Assert(!safeDiagnostic.Contains('\\n') && !safeDiagnostic.Contains('\\r'),
    "diagnostic sanitizer retained control characters");

Console.WriteLine("ASF plugin command dispatch, MEF registration, and runtime compatibility smoke tests passed.");
