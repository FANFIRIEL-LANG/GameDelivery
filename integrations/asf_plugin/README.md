# GameDelivery ASF plugin — preview and guarded trade delivery

The plugin exposes three commands through ASF's custom command handler:

- `gamedelivery-diagnose <BotName>` keeps the legacy check for plugin registration, runtime ASF/SteamKit assemblies, method signatures, connection state, and the bot's read-only inventory.
- `gamedelivery-diagnose <BotName> <AppID...> [--recipient <SteamID64>]` reads badge metadata for the selected ASF session and, when a recipient is supplied, verifies the recipient's public profile identity before reading their badge and inventory.
- `gamedelivery-preview` re-checks a proposed order against the current ASF inventory and **never sends**.
- `gamedelivery-send` is used only after GameDelivery's order screen repeats the live preview, shows the exact selected assets, and receives explicit operator confirmation.

The command handler is compatible with ASF 6.3.11.7: the command prefix is optional in the ASF console and IPC, and used when sending commands through Steam chat. At startup version 1.0.2 logs whether ASF's active plugin collection includes this exact `IBotCommand2` handler. Recognized callbacks log only command name and argument count, never the encoded payload or trade token.

A CI build does not prove that sending works against the ASF instance installed on a customer's PC. Validate this integration with a controlled test account and a low-risk test order before handling real sales.

## Read-only badge diagnostics

Supply one or more canonical decimal AppIDs (up to 20). The plugin reads `/my/ajaxgetbadgeinfo/{appid}` through the selected bot's ASF session. With `--recipient`, it first requests the profile XML and compares its `steamID64` to the supplied ID; if identity cannot be verified, it stops before requesting recipient badge or inventory data. The XML endpoint is deprecated by Steam, so unavailable or private data remains an error/unknown state and is never converted to badge level zero.

For a verified recipient, the plugin reads `/profiles/{steamid64}/ajaxgetbadgeinfo/{appid}` and fully enumerates ASF's Steam Community inventory (`753/6`). It groups normal trading cards by `RealAppID` and exact `MarketHashName`, includes non-tradable cards and `Amount`, and reports foil, other, and unknown item types separately. Matching is exact and case-sensitive. Every result is diagnostic-only and states `sendEnabled: false` and `tradeOfferCreated: false`.

## Install the CI package

The GitHub Actions artifact is named `GameDelivery-ASFPlugin`. It contains the plugin DLL, this README, and `BUILD_INFO.txt`.

1. Download the artifact from a successful **ASF plugin** workflow run.
2. Stop ASF and extract the plugin folder into ASF's `plugins` directory.
3. Confirm the resulting path is `plugins/GameDelivery.ASFPlugin/GameDelivery.ASFPlugin.dll`.
4. Start ASF through GameDelivery and check its console/log for `GameDeliveryPlugin` being loaded.

The plugin is compiled against the official ASF 6.3.11.7 release assemblies. Its System.Text.Json options explicitly configure DefaultJsonTypeInfoResolver because ASF may disable the framework's implicit reflection resolver. The plugin and smoke test build with JsonSerializerIsReflectionEnabledByDefault=false. JSON parsing and serialization use `.NET 10 System.Text.Json`, which is part of the shared framework; the plugin has no Newtonsoft.Json or other external JSON runtime dependency. The CI build verifies the official `ASF-generic.zip` SHA-256 and rejects a plugin output/package that bundles host-shared assemblies (`ArchiSteamFarm.dll`, `SteamKit2.dll`, or `System.Composition.AttributedModel.dll`). Install this package with ASF 6.3.11.7. The plugin package intentionally contains only `GameDelivery.ASFPlugin.dll`, this README, and `BUILD_INFO.txt`; ASF supplies its own shared runtime dependencies. For another ASF version, rebuild against that exact installation using the instructions below.

## What preview does

GameDelivery calls local, authenticated `POST /Api/Command` with `gamedelivery-preview <base64url-json>`. The plugin:

- requires ASF Owner access;
- resolves the requested bot in the running ASF instance and requires it to be online;
- fetches the current inventory through ASF's Actions API;
- verifies every requested asset exists and is tradable in Steam Community inventory (AppID 753/context 6). This includes trading cards and other tradable items in that inventory. Game inventories such as CS2 (AppID 730/context 2) are outside this trade path;
- returns a preview bound to the order, recipient, and exact ordered asset list;
- returns `sendEnabled: false`.

## Guarded sending flow

Only the GameDelivery order screen calls `gamedelivery-send` after an explicit confirmation:

- The plugin accepts commands only from ASF Owner.
- The target must be an individual Steam account and must not be the sending bot itself.
- The plugin re-fetches the live inventory immediately before creating the offer and rejects missing, untradable, duplicate, or assets outside Steam Community inventory (AppID 753/context 6).
- The one-use command payload contains a random nonce, not the recipient trade token. GameDelivery stores the token in a one-use JSON file under the current user's LocalAppData; the plugin verifies the file's order, bot, recipient, nonce, and exact asset list and then deletes the file.
- The plugin writes a persistent record keyed by order ID with a fingerprint of the order, recipient, bot, and assets. Reusing the order with a different payload is refused. Repeated calls for a recorded offer do not create a second offer. If the outcome is unknown, the plugin blocks an automatic retry and instructs the operator to inspect Steam's outgoing offers.
- The result contains the offer ID(s) and whether an ASF/mobile confirmation remains pending. This does **not** mean the recipient has accepted the offer.
- The plugin does not automatically accept arbitrary incoming offers.

The plugin's idempotency record is stored under `%LOCALAPPDATA%\GameDelivery\ASFPlugin\sent_offers.json`. One-use secrets are stored temporarily under `%LOCALAPPDATA%\GameDelivery\pending_trade_requests`. Keep the Windows account and its LocalAppData protected. The trade token is not put in the ASF command payload or the GameDelivery journal.

## Build against a local ASF version

If the installed ASF version does not match the CI package's ASF API, build against that installation's binaries instead:

```powershell
dotnet build integrations\asf_plugin\GameDelivery.ASFPlugin.csproj -p:ASF_BIN_DIR=C:\ASF\generic -c Release
```

Replace `C:\ASF\generic` with the directory containing the intended ASF Generic assemblies. Copy only `GameDelivery.ASFPlugin.dll` into `plugins/GameDelivery.ASFPlugin`, then restart ASF.
