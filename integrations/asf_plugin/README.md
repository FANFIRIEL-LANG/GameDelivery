# GameDelivery ASF plugin — preview and guarded trade delivery

The plugin has two separate commands:

- `gamedelivery-preview` re-checks a proposed order against the current ASF inventory and **never sends**.
- `gamedelivery-send` is used only after GameDelivery's order screen repeats the live preview, shows the exact selected assets, and receives explicit operator confirmation.

A CI build does not prove that sending works against the ASF instance installed on a customer's PC. Validate this integration with a controlled test account and a low-risk test order before handling real sales.

## Install the CI package

The GitHub Actions artifact is named `GameDelivery-ASFPlugin`. It contains the plugin DLL, this README, and `BUILD_INFO.txt`.

1. Download the artifact from a successful **ASF plugin** workflow run.
2. Stop ASF and extract the plugin folder into ASF's `plugins` directory.
3. Confirm the resulting path is `plugins/GameDelivery.ASFPlugin/GameDelivery.ASFPlugin.dll`.
4. Start ASF through GameDelivery and check its console/log for `GameDeliveryPlugin` being loaded.

The plugin is built against the ASF source commit recorded in `BUILD_INFO.txt`. Match the build to the ASF version installed on the target machine. Do not copy `ArchiSteamFarm.dll`, `SteamKit2.dll`, or other ASF assemblies from the build into the installed ASF folder.

## What preview does

GameDelivery calls local, authenticated `POST /Api/Command` with `gamedelivery-preview <base64url-json>`. The plugin:

- requires ASF Owner access;
- resolves the requested bot in the running ASF instance and requires it to be online;
- fetches the current inventory through ASF's Actions API;
- verifies every requested asset exists, is tradable, and is a normal Steam trading card in AppID 753/context 6;
- returns a preview bound to the order, recipient, and exact ordered asset list;
- returns `sendEnabled: false`.

## Guarded sending flow

Only the GameDelivery order screen calls `gamedelivery-send` after an explicit confirmation:

- The plugin accepts commands only from ASF Owner.
- The target must be an individual Steam account and must not be the sending bot itself.
- The plugin re-fetches the live inventory immediately before creating the offer and rejects missing, untradable, duplicate, or non-standard card assets.
- The one-use command payload contains a random nonce, not the recipient trade token. GameDelivery stores the token in a one-use JSON file under the current user's LocalAppData; the plugin verifies the file's order, bot, recipient, nonce, and exact asset list and then deletes the file.
- The plugin writes a persistent record keyed by order ID with a fingerprint of the order, recipient, bot, and assets. Reusing the order with a different payload is refused. Repeated calls for a recorded offer do not create a second offer. If the outcome is unknown, the plugin blocks an automatic retry and instructs the operator to inspect Steam's outgoing offers.
- The result contains the offer ID(s) and whether an ASF/mobile confirmation remains pending. This does **not** mean the recipient has accepted the offer.
- The plugin does not automatically accept arbitrary incoming offers.

The plugin's idempotency record is stored under `%LOCALAPPDATA%\GameDelivery\ASFPlugin\sent_offers.json`. One-use secrets are stored temporarily under `%LOCALAPPDATA%\GameDelivery\pending_trade_requests`. Keep the Windows account and its LocalAppData protected. The trade token is not put in the ASF command payload or the GameDelivery journal.

## Build against a local ASF version

If the installed ASF version does not match the CI package's ASF API, build against that installation's binaries instead:

```powershell
dotnet build integrations\asf_plugin\GameDelivery.ASFPlugin.csproj -p:ASF_BIN_DIR=C:\ASF\win-x64 -c Release
```

Replace `C:\ASF\win-x64` with the directory containing the installed ASF assemblies. Copy only `GameDelivery.ASFPlugin.dll` into `plugins/GameDelivery.ASFPlugin`, then restart ASF.
