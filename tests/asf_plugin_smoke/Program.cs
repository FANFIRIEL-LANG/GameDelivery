using ArchiSteamFarm.Plugins.Interfaces;
using GameDelivery.ASFPlugin;
using System;
using System.Linq;

static void Assert(bool condition, string message)
{
    if (!condition)
    {
        throw new InvalidOperationException(message);
    }
}

string[] tokens;

Assert(
    CommandArgumentParser.TryNormalize(
        "gamedelivery-diagnose Ewaylariell",
        ["gamedelivery-diagnose", "Ewaylariell"],
        "!",
        out tokens),
    "plain ASF console command was not recognized");
Assert(tokens.SequenceEqual(["gamedelivery-diagnose", "Ewaylariell"]), "plain command arguments changed");

Assert(
    CommandArgumentParser.TryNormalize(
        "!gamedelivery-diagnose Ewaylariell",
        ["!gamedelivery-diagnose", "Ewaylariell"],
        "!",
        out tokens),
    "prefixed command was not recognized");
Assert(tokens.SequenceEqual(["gamedelivery-diagnose", "Ewaylariell"]), "prefix was not stripped exactly");

Assert(
    CommandArgumentParser.TryNormalize(
        "Ewaylariell gamedelivery-diagnose",
        ["Ewaylariell", "gamedelivery-diagnose"],
        "!",
        out tokens),
    "command following a bot selector was not recognized");
Assert(tokens.SequenceEqual(["gamedelivery-diagnose", "Ewaylariell"]), "bot selector was not normalized");

Assert(
    CommandArgumentParser.TryNormalize(
        "gamedelivery-preview eyJvcmRlcklkIjoiMSJ9",
        ["gamedelivery-preview", "eyJvcmRlcklkIjoiMSJ9"],
        "!",
        out tokens),
    "preview payload command was not recognized");
Assert(tokens.Length == 2 && tokens[1] == "eyJvcmRlcklkIjoiMSJ9", "preview payload was modified");

Assert(
    !CommandArgumentParser.TryNormalize("status Ewaylariell", ["status", "Ewaylariell"], "!", out _),
    "unrelated ASF command was intercepted");

Assert(typeof(IPlugin).IsAssignableFrom(typeof(GameDeliveryPlugin)),
    "plugin does not implement the exact IPlugin interface from the pinned ASF assembly");
Assert(typeof(IASF).IsAssignableFrom(typeof(GameDeliveryPlugin)),
    "plugin does not implement the exact IASF interface from the pinned ASF assembly");
Assert(typeof(IBotCommand2).IsAssignableFrom(typeof(GameDeliveryPlugin)),
    "plugin does not implement the exact IBotCommand2 interface from the pinned ASF assembly");
Assert(typeof(IBotCommand2).GetInterfaceMap(typeof(GameDeliveryPlugin)).TargetMethods
        .Any(method => method.Name == "OnBotCommand"),
    "IBotCommand2.OnBotCommand has no implementation mapping");
Assert(typeof(GameDeliveryPlugin).GetCustomAttributesData()
        .Any(attribute => attribute.AttributeType.FullName == "System.Composition.ExportAttribute"),
    "plugin has no MEF Export attribute");
Assert(typeof(GameDeliveryPlugin).Assembly.GetName().Version == new Version(1, 0, 1, 0),
    "plugin assembly version is not the expected fixed version 1.0.1.0");

Console.WriteLine("ASF plugin command dispatch and runtime compatibility smoke tests passed.");
