# GameDelivery ASF plugin — trade draft preview

**Preview-only.** This plugin checks a prepared GameDelivery order against the current ASF inventory. It does not create or send a Steam trade offer, accept/cancel offers, or confirm Steam Guard requests.

## Install the CI package

The GitHub Actions artifact is named `GameDelivery-ASFPlugin-preview-only`. It contains the plugin DLL and build information.

1. Download the artifact from a successful **ASF plugin** workflow run.
2. Stop ASF and extract the artifact ZIP into ASF's `plugins` directory.
3. Confirm the resulting path is `plugins/GameDelivery.ASFPlugin/GameDelivery.ASFPlugin.dll`.
4. Start ASF and check its log for `GameDeliveryPlugin` being loaded.

ASF's plugin system gives loaded plugins control over the ASF process. Only install this plugin from the GameDelivery repository you trust. Do not copy `ArchiSteamFarm.dll`, `SteamKit2.dll`, or other ASF assemblies from the build into the installed ASF folder.

The CI package is built against the ASF source commit recorded in `BUILD_INFO.txt`. The plugin has passed compilation, but the preview command still needs to be confirmed against the ASF instance installed on the target computer before this integration can be marked fully verified.

## Build against a local ASF version

If the installed ASF version does not match the CI package's ASF API, build against that installation's binaries instead:

```powershell
dotnet build integrations\asf_plugin\GameDelivery.ASFPlugin.csproj -p:ASF_BIN_DIR=C:\ASF\win-x64 -c Release
```

Replace `C:\ASF\win-x64` with the directory containing the installed ASF assemblies. Copy only `GameDelivery.ASFPlugin.dll` into `plugins/GameDelivery.ASFPlugin`, then restart ASF.

## How GameDelivery calls the plugin

GameDelivery calls the local, authenticated `POST /Api/Command` endpoint with the command `gamedelivery-preview <base64url-json>`. The JSON contains the order ID, bot name, recipient SteamID64 derived from the validated trade URL, and exact asset IDs. The trade URL's access token is not included in the plugin command.

The plugin:

- requires ASF Owner access;
- resolves the requested bot in the running ASF instance and requires it to be online;
- fetches the current inventory through ASF's Actions API;
- verifies every requested asset exists, is tradable, and is a normal Steam trading card in AppID 753/context 6;
- returns a preview bound to the order, recipient, and ordered asset list;
- always returns `sendEnabled: false`.

## What is not included yet

The live preview command has not been verified against the target computer's ASF instance. Actual trade creation, duplicate-send protection, offer-ID persistence, and Steam Guard confirmation remain a separate planned stage. No Steam trade is sent by this plugin.
