# GameDelivery ASF plugin — trade draft preview

This plugin deliberately provides a **preview-only** command. It does not call
`SendTradeOffer`, does not accept/cancel offers, and does not confirm mobile
authenticator requests.

## Why this plugin exists

The stock ASF IPC API exposes inventory/configuration operations but no endpoint
that creates an arbitrary offer for selected asset IDs and a buyer trade URL.
ASF supports custom commands through `IBotCommand2`; this plugin uses that
documented extension point and the existing `POST /Api/Command` IPC route.

## Build

Build against the same ASF release that is installed locally. The output folder
must contain `ArchiSteamFarm.dll`, `SteamKit2.dll`, and
`System.Composition.AttributedModel.dll`.

```powershell
dotnet build integrations\asf_plugin\GameDelivery.ASFPlugin.csproj -p:ASF_BIN_DIR=C:\ASF\win-x64 -c Release
```

Replace `C:\ASF\win-x64` with the local ASF output folder. Copy only
`GameDelivery.ASFPlugin.dll` from the build output into ASF's `plugins`
directory, then restart ASF. Do not copy replacement ASF/SteamKit assemblies
over the installed version.

## Command contract

GameDelivery calls the local, authenticated `POST /Api/Command` endpoint with
the command `gamedelivery-preview <base64url-json>`. The JSON contains order ID,
bot name, recipient SteamID64 derived from the validated trade URL, and exact
asset IDs. The trade URL's access token is not included in the plugin request.

The plugin:
- requires ASF Owner access;
- resolves the target bot in the running ASF instance and requires it to be online;
- fetches the current inventory through that bot's ASF Actions API;
- verifies every requested asset exists, is tradable, and is a normal Steam
  trading card in AppID 753/context 6;
- returns a JSON preview bound to the order, recipient and ordered asset list;
- always returns `sendEnabled: false`.

## Scope and limitations

The Python-side client and tests are covered by GameDelivery CI. This C# plugin
must be compiled against the user's matching ASF binaries before installation;
a Python CI pass does not claim that the plugin has been compiled or loaded.
The live preview command is not verified until it is called against the installed
ASF instance. Actual trade creation, duplicate-send protection, offer-ID
persistence and Steam Guard confirmation are separate planned work (step 24).
