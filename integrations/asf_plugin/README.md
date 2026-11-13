# GameDelivery ASF plugin — preview and guarded trade delivery

The plugin exposes four commands through ASF's custom command handler:

- `gamedelivery-diagnose <BotName>` keeps the legacy check for plugin registration, runtime ASF/SteamKit assemblies, method signatures, connection state, and the bot's read-only inventory.
- `gamedelivery-diagnose <BotName> <AppID...> [--recipient <SteamID64>]` reads badge metadata for the selected ASF session and, when a recipient is supplied, verifies the recipient's public profile identity before reading their badge and inventory.
- `gamedelivery-preview` re-checks a proposed order against the current ASF inventory and **never sends**.
- `gamedelivery-send` is used only after GameDelivery's order screen repeats the live preview, shows the exact selected assets, and receives explicit operator confirmation.
- `gamedelivery-status <BotName> <TradeOfferID>` reads one exact outgoing offer through the selected bot's ASF `ArchiWebHandler`; a not-found response is inconclusive and must not be treated as cancellation.

The command handler is compatible with ASF 6.3.11.7: the command prefix is optional in the ASF console and IPC, and used when sending commands through Steam chat. At startup version 1.0.2 logs whether ASF's active plugin collection includes this exact `IBotCommand2` handler. Recognized callbacks log only command name and argument count, never the encoded payload or trade token.

A CI build does not prove that sending works against the ASF instance installed on a customer's PC. Validate this integration with a controlled test account and a low-risk test order before handling real sales.

## Read-only badge diagnostics

Supply one or more canonical decimal AppIDs (up to 20). The plugin reads `/my/ajaxgetbadgeinfo/{appid}` through the selected bot's ASF session. With `--recipient`, it first requests the profile XML and compares its `steamID64` to the supplied ID; if identity cannot be verified, it stops before requesting recipient badge or inventory data. The XML endpoint is deprecated by Steam, so unavailable or private data remains an error/unknown state and is never converted to badge level zero.

For a verified recipient, the plugin reads `/profiles/{steamid64}/ajaxgetbadgeinfo/{appid}` and fully enumerates ASF's Steam Community inventory (`753/6`). It groups normal trading cards by `RealAppID` and exact `MarketHashName`, includes non-tradable cards and `Amount`, and reports foil, other, and unknown item types separately. Matching is exact and case-sensitive. Every result is diagnostic-only and states `sendEnabled: false` and `tradeOfferCreated: false`.

## Safely prepare a separate Steam test profile

Create the account yourself through [Steam's account creation flow](https://help.steampowered.com/en/faqs/view/1DED-79C6-0568-A72C). Use a dedicated email address, a unique password stored in your password manager, a non-identifying profile name, and Steam Guard. Keep the password, Guard codes, cookies, tokens, and authenticator files on your device; never send them with test results. Steam documents account creation and Steam Guard [here](https://help.steampowered.com/en/faqs/view/06b0-26e6-2cf8-254c).

In **Profile → Edit Profile → Privacy Settings**, set **My profile**, **Game details**, and **Inventory** to **Public** for the test. Leave **Friends List** private; the diagnostic does not need it. Make sure the selected game is not individually marked private. Steam exposes inventory privacy separately, and a public inventory can be viewed by anyone ([profile privacy](https://help.steampowered.com/en/faqs/view/588C-C67D-0251-C276), [inventory visibility](https://help.steampowered.com/en/faqs/view/46A2-2B3C-95CC-8878)). Steam also says private games hide ownership details and stop card drops ([Steam Private Games](https://help.steampowered.com/en/faqs/view/1150-C06F-4D62-4966)). Use only the dedicated test account for public inventory data.

Add that test account locally to the prepared isolated ASF copy, not to `D:\ASF-generic` or the active ASF installation. The current isolated profile has no bot and `Headless=true`; before using the ASF console, set `Headless=false` in that test copy, keep `IPC=false`, and start it in a local terminal. Do not share its bot config file. `BotName` is the connected bot's name in that isolated ASF instance; `AppID` is the decimal Steam AppID whose badge data is being checked; `SteamID64` is the public recipient ID from a numeric `/profiles/<SteamID64>` URL. The bot and recipient may be the same test account. Choose an AppID with badge data visible to that account. An empty inventory still verifies the read endpoint; local smoke tests cover synthetic normal, foil, non-tradable, duplicate, and missing-card cases.

Run only `gamedelivery-diagnose <BotName> <AppID> --recipient <SteamID64>`. The diagnostic reads badge responses and enumerates inventory through ASF; its command branch returns before `TradeDelivery.ProcessSendCommand`, and the diagnostic methods contain no `SendTradeOffer`, confirmation, or `TradeDelivery` invocations. The legacy diagnostic reflects the trade-send method signature for compatibility reporting but does not invoke it. It does not modify inventory or create offers. Do not run `gamedelivery-send` for this test. Share only `BotName`, `AppID`, `SteamID64`, and confirmation that the test bot is connected and the profile/inventory are public. Never share credentials, bot config, Steam Guard data, cookies, tokens, or maFile.

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
- accepts only Steam Community inventory (AppID 753/context 6) or CS2 inventory (AppID 730/context 2);
- fetches the exact requested inventory through ASF's Actions API;
- verifies every requested asset belongs to that exact AppID/context, has a description, and has `Tradable == true`; Steam Community assets must additionally be normal (non-foil) Trading Cards, while CS2 items use the separate 730/2 path;
- returns a preview bound to the order, recipient, and exact ordered asset list;
- returns `sendEnabled: false`.

## Guarded sending flow

Only the GameDelivery order screen calls `gamedelivery-send` after an explicit confirmation:

- The plugin accepts commands only from ASF Owner.
- The target must be an individual Steam account and must not be the sending bot itself.
- The plugin re-fetches the live inventory immediately before creating the offer and rejects missing, untradable, duplicate, mismatched-AppID/context, or undescribed assets. The 753/6 path additionally rejects foil and non-card items; the 730/2 path is CS2-specific.
- Before creating the offer, the plugin uses `ArchiWebHandler.GetInventoryAsync` to confirm that the recipient inventory is readable in the same AppID/context. It then queries `IEconService/GetTradeHoldDurations` with the sending bot's public ASF OAuth `AccessToken` and ASF's own `SteamConfiguration.HttpClientFactory`; no Steam Web API key is used. A positive hold or missing/unknown result blocks sending. This pre-flight can verify inventory visibility and reported escrow duration, but it cannot certify every possible Steam trade restriction; any restriction not represented by that endpoint may still be returned by Steam when `SendTradeOffer` is attempted.
- The one-use command payload contains a random nonce, not the recipient trade token. GameDelivery stores the token in a one-use JSON file under the current user's LocalAppData; the plugin verifies the file's order, bot, recipient, nonce, and exact asset list and then deletes the file.
- The plugin writes a persistent record keyed by order ID with a fingerprint of the order, recipient, bot, and assets. Reusing the order with a different payload is refused. Repeated calls for a recorded offer do not create a second offer. If the outcome is unknown, the plugin blocks an automatic retry and instructs the operator to inspect Steam's outgoing offers.
- The result contains the offer ID(s) and whether an ASF/mobile confirmation remains pending. This does **not** mean the recipient has accepted the offer. Use `gamedelivery-status <BotName> <TradeOfferID>` to read the current outgoing offer state and exact `items_to_give` manifest through ASF.
- `SendTradeOffer` is called with an empty `customMessage`, so GameDelivery adds no extra suffix. ASF 6.3.11.7 itself still adds its fixed `Sent by ArchiSteamFarm...` identifier; the plugin argument cannot remove that ASF-owned signature.
- The plugin does not automatically accept arbitrary incoming offers.

The plugin's idempotency record is stored under `%LOCALAPPDATA%\GameDelivery\ASFPlugin\sent_offers.json`. One-use secrets are stored temporarily under `%LOCALAPPDATA%\GameDelivery\pending_trade_requests`. Keep the Windows account and its LocalAppData protected. The trade token is not put in the ASF command payload or the GameDelivery journal.

## Build against a local ASF version

If the installed ASF version does not match the CI package's ASF API, build against that installation's binaries instead:

```powershell
dotnet build integrations\asf_plugin\GameDelivery.ASFPlugin.csproj -p:ASF_BIN_DIR=C:\ASF\generic -c Release
```

Replace `C:\ASF\generic` with the directory containing the intended ASF Generic assemblies. Copy only `GameDelivery.ASFPlugin.dll` into `plugins/GameDelivery.ASFPlugin`, then restart ASF.
