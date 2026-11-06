using ArchiSteamFarm.Plugins.Interfaces;
using GameDelivery.ASFPlugin;
using System;
using System.Composition;
using System.Composition.Convention;
using System.Composition.Hosting;
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
        "gamedelivery-diagnose Ewaylariell",
        ["unparsed"],
        "!",
        out tokens),
    "raw-message fallback was not used when args did not contain the custom command");
Assert(tokens.SequenceEqual(["gamedelivery-diagnose", "Ewaylariell"]), "raw-message fallback changed command arguments");

Assert(
    CommandArgumentParser.TryNormalize(
        "gamedelivery-preview eyJvcmRlcklkIjoiMSJ9",
        ["gamedelivery-preview", "eyJvcmRlcklkIjoiMSJ9"],
        "!",
        out tokens),
    "preview payload command was not recognized");
Assert(tokens.Length == 2 && tokens[1] == "eyJvcmRlcklkIjoiMSJ9", "preview payload was modified");

Assert(
    !CommandArgumentParser.TryNormalize("status gamedelivery-diagnose", ["status", "gamedelivery-diagnose"], "!", out _),
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

Console.WriteLine("ASF plugin command dispatch, MEF registration, and runtime compatibility smoke tests passed.");
