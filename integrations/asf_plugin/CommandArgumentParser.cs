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

        for (int commandIndex = 0; commandIndex < tokens.Length; commandIndex++)
        {
            string candidate = tokens[commandIndex];
            if (!string.IsNullOrEmpty(commandPrefix)
                && candidate.StartsWith(commandPrefix, StringComparison.Ordinal))
            {
                candidate = candidate[commandPrefix.Length..];
            }

            if (!CommandNames.Contains(candidate))
            {
                continue;
            }

            List<string> result = new(tokens.Length)
            {
                candidate
            };

            for (int index = 0; index < tokens.Length; index++)
            {
                if (index != commandIndex)
                {
                    result.Add(tokens[index]);
                }
            }

            normalizedArgs = result.ToArray();
            return true;
        }

        return false;
    }
}
