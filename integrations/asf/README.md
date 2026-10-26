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
