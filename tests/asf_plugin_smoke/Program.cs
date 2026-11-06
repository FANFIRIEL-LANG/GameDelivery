using ArchiSteamFarm.Plugins.Interfaces;
using GameDelivery.ASFPlugin;

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

Assert(
    typeof(IBotCommand2).IsAssignableFrom(typeof(GameDeliveryPlugin)),
    "plugin does not implement the exact IBotCommand2 interface from the pinned ASF assembly");

Console.WriteLine("ASF plugin command dispatch smoke tests passed.");
