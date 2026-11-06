using System;
using System.Collections.Generic;
using System.Linq;

namespace GameDelivery.ASFPlugin;

internal static class CommandArgumentParser
{
    private static readonly HashSet<string> CommandNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "gamedelivery-diagnose",
        "gamedelivery-preview",
        "gamedelivery-send"
    };

    public static bool TryNormalize(
        string? message,
        string[]? args,
        string? commandPrefix,
        out string[] normalizedArgs)
    {
        if (TryNormalizeTokens(args, commandPrefix, out normalizedArgs))
        {
            return true;
        }

        if (string.IsNullOrWhiteSpace(message))
        {
            normalizedArgs = [];
            return false;
        }

        string[] messageTokens = message.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        return TryNormalizeTokens(messageTokens, commandPrefix, out normalizedArgs);
    }

    private static bool TryNormalizeTokens(
        IEnumerable<string>? source,
        string? commandPrefix,
        out string[] normalizedArgs)
    {
        normalizedArgs = [];
        if (source is null)
        {
            return false;
        }

        string[] tokens = source
            .Where(token => !string.IsNullOrWhiteSpace(token))
            .Select(token => token.Trim())
            .ToArray();

        if (tokens.Length == 0)
        {
            return false;
        }

        string commandName = tokens[0];
        if (!string.IsNullOrEmpty(commandPrefix)
            && commandName.StartsWith(commandPrefix, StringComparison.Ordinal))
        {
            commandName = commandName[commandPrefix.Length..];
        }

        if (!CommandNames.Contains(commandName))
        {
            return false;
        }

        tokens[0] = commandName;
        normalizedArgs = tokens;
        return true;
    }
}
