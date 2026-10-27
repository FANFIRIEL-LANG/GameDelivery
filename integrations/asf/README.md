# ASF integration

GameDelivery uses the official ArchiSteamFarm (ASF) process as the Steam-facing
component. Steam account login/session handling stays in ASF; GameDelivery must
not collect Steam passwords or persist ASF secrets in source control.

## Connection model

- Connect to ASF IPC on the same PC, default address `http://127.0.0.1:1242`.
- Enable IPC and set a non-empty `IPCPassword` in the local ASF configuration.
- Keep IPC bound to localhost. Do not expose port 1242 to the internet or LAN.
- Enter the password only in GameDelivery. It can optionally be saved in the
  operating-system credential store; it is never written to SQLite, source code,
  logs, GitHub issues, or diagnostic summaries.
- From the `ASF-боты` tab, select the ASF config directory and, if needed, the
  executable once. GameDelivery remembers those paths, can auto-start ASF,
  shows its console output and accepts console input, and can request shutdown.
- The bot table merges valid per-bot JSON files from the selected config directory
  with bots returned by IPC. Disabled accounts remain visible when ASF reports
  no active bots.
- Use the running ASF instance's `/swagger` page as the source of truth for
  endpoint schemas for the installed version.

## Per-account proxy

The initial configuration model is in `proxy_config.py`. It maps one proxy to
one ASF bot and builds the documented bot settings:

- `WebProxy`, `WebProxyUsername`, and `WebProxyPassword` are per-bot
  settings for ASF HTTP(S) requests to Steam web services.
- `SteamProtocols = 4` is a **global ASF setting in the installed ASF
  V6.3.10.6 schema**, not a bot setting. The version-tagged `GlobalConfig.cs`
  defines it, while `BotConfig.cs` does not expose it. WebSocket-only transport
  is required when Steam client connections must also use `WebProxy`. Changing
  it affects every bot in that ASF instance and may reduce connection fallback
  options. If the user upgrades ASF, re-check the installed version's schema
  before applying settings.

The app must never write `SteamProtocols` into a bot config or silently change
the global setting. The client has a separate opt-in method that reads the
current global config, changes only `SteamProtocols`, posts it through the
official `POST /Api/ASF` endpoint, then reads the value back for verification.
The UI now requires two explicit confirmations before this instance-wide
change. The UI must not call both traffic paths active until real connection
behavior has been verified. A web proxy check alone is not proof
that Steam CM traffic uses the proxy.

Proxy credentials must not be embedded in the proxy URI, logs, GitHub, or
diagnostic summaries. The current UI stores proxy metadata in local SQLite and
stores the proxy password through the operating-system keyring (`keyring`); it
fails closed when a usable keyring backend is unavailable. IPCPassword can be
kept only in memory or explicitly saved in the operating-system keyring; the
remember option fails closed when no secure backend is available. The secret is
sent only in authenticated local ASF IPC requests.

## Scope and safeguards

The Python adapter provides local IPC transport, process/bot status calls,
per-bot proxy configuration with read-back verification, an explicitly
confirmed global `SteamProtocols` update with read-back verification, and
graceful process shutdown via local IPC. The `ASF-боты` tab can start ASF,
optionally auto-start it with GameDelivery, display its console and forward
console input, restart it with IPC reconnect, and stop a process started by
GameDelivery when the app exits. Configured accounts are merged with the IPC bot
map so disabled bot JSON files remain visible. New configs default to
`Enabled: false` and never include a Steam password. A process started with
`--process-required` remains available for IPC when all bots are disabled.
Inventory pagination, item selection, trade offer construction, and automatic
sending are not implemented yet; these require verifying exact IPC endpoints
and request schemas for the selected ASF version.

Inventory data must be cached locally with per-bot ownership and refreshed from
ASF; stale inventory must not be treated as available stock. Before any outgoing
trade, GameDelivery must reserve the exact asset IDs transactionally, validate
the destination trade URL and item tradability, show a review step, and record
the resulting offer ID/status. Steam Guard / mobile confirmations remain
mandatory where Steam requires them. No automatic acceptance of arbitrary
incoming trades is enabled by this integration.

ASF's built-in trading features and commands have specific constraints; they
must not be treated as a generic arbitrary-item delivery API without verifying
the exact supported workflow. Do not install third-party ASF plugins without
reviewing their source, compatibility, and permissions.
