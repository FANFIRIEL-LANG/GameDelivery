# ASF integration

GameDelivery uses the official ArchiSteamFarm (ASF) process as the Steam-facing
component. Steam account login/session handling stays in ASF; GameDelivery must
not collect Steam passwords or persist ASF secrets in source control.

## Connection model

- Connect to ASF IPC on the same PC, default address `http://127.0.0.1:1242`.
- Enable IPC and set a non-empty `IPCPassword` in the local ASF configuration.
- Keep IPC bound to localhost. Do not expose port 1242 to the internet or LAN.
- Supply the password through local application settings, never source code,
  logs, GitHub issues, or chat.
- Use the running ASF instance's `/swagger` page as the source of truth for
  endpoint schemas for the installed version.

## Per-account proxy

The initial configuration model is in `proxy_config.py`. It maps one proxy to
one ASF bot and builds the documented bot settings:

- `WebProxy`, `WebProxyUsername`, and `WebProxyPassword` are per-bot
  settings for ASF HTTP(S) requests to Steam web services.
- `SteamProtocols = 4` is a **global ASF setting**, not a bot setting. ASF
  documents WebSocket-only transport as the requirement when Steam client
  connections must also use `WebProxy`. Changing it affects every bot in that
  ASF instance and may reduce connection fallback options.

The app must never write `SteamProtocols` into a bot config or silently change
the global setting. The client has a separate opt-in method that reads the
current global config, changes only `SteamProtocols`, posts it through the
official `POST /Api/ASF` endpoint, then reads the value back for verification.
The UI now requires two explicit confirmations before this instance-wide
change. The UI must not call both traffic paths active until real connection
behavior has been verified. A web proxy check alone is not proof
that Steam CM traffic uses the proxy.

Proxy credentials must not be embedded in the proxy URI, logs, GitHub, or
diagnostic summaries. The model returns secrets only in the explicit ASF
configuration payload. Before the UI stores credentials, implement protected
local secret storage and ensure secrets are never serialized into ordinary
settings or logs.

## Scope and safeguards

The Python adapter currently provides a small HTTP transport and process/bot
status calls. It does not yet implement inventory pagination, item selection,
trade offer construction, or automatic sending. These require verifying the
exact IPC endpoints and request schemas for the selected ASF version.

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
